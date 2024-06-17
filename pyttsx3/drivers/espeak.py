import os
import wave
import platform
import ctypes
import time
import subprocess
from tempfile import NamedTemporaryFile
if platform.system() == 'Windows':
    import winsound

from ..voice import Voice
from . import _espeak, fromUtf8, toUtf8


# noinspection PyPep8Naming
def buildDriver(proxy):
    return EspeakDriver(proxy)


# noinspection PyPep8Naming
class EspeakDriver(object):
    _moduleInitialized = False
    _defaultVoice = ''

    def __init__(self, proxy):
        print("[EspeakDriver.__init__] Initializing eSpeak Driver")
        if not EspeakDriver._moduleInitialized:
            # espeak cannot initialize more than once per process and has
            # issues when terminating from python (assert error on close)
            # so just keep it alive and init once        
            rate = _espeak.Initialize(_espeak.AUDIO_OUTPUT_RETRIEVAL, 1000)
            if rate == -1:
                raise RuntimeError('could not initialize espeak')
            EspeakDriver._defaultVoice = 'default'
            EspeakDriver._moduleInitialized = True
        self._proxy = proxy
        self._looping = False
        self._stopping = False
        self._speaking = False
        self._text_to_say = None
        self._data_buffer = b''
        self._numerise_buffer = []

        _espeak.SetSynthCallback(self._onSynth)
        self.setProperty('voice', EspeakDriver._defaultVoice)
        self.setProperty('rate', 200)
        self.setProperty('volume', 1.0)

    def numerise(self, data):
        self._numerise_buffer.append(data)
        return ctypes.c_void_p(len(self._numerise_buffer))

    def decode_numeric(self, data):
        return self._numerise_buffer[int(data) - 1]

    @staticmethod
    def destroy():
        _espeak.SetSynthCallback(None)

    def stop(self):
        if _espeak.IsPlaying():
            self._stopping = True
            _espeak.Cancel()

    @staticmethod
    def getProperty(name: str):
        if name == 'voices':
            voices = []
            for v in _espeak.ListVoices(None):
                kwargs = {'id': fromUtf8(v.name), 'name': fromUtf8(v.name)}
                if v.languages:
                    kwargs['languages'] = [v.languages]
                genders = [None, 'male', 'female']
                kwargs['gender'] = genders[v.gender]
                kwargs['age'] = v.age or None
                voices.append(Voice(**kwargs))
            return voices
        elif name == 'voice':
            voice = _espeak.GetCurrentVoice()
            return fromUtf8(voice.contents.name)
        elif name == 'rate':
            return _espeak.GetParameter(_espeak.RATE)
        elif name == 'volume':
            return _espeak.GetParameter(_espeak.VOLUME) / 100.0
        elif name == 'pitch':
            return _espeak.GetParameter(_espeak.PITCH)
        else:
            raise KeyError('unknown property %s' % name)

    @staticmethod
    def setProperty(name: str, value):
        if name == 'voice':
            if value is None:
                return
            try:
                utf8Value = toUtf8(value)
                _espeak.SetVoiceByName(utf8Value)
            except ctypes.ArgumentError as e:
                raise ValueError(str(e))
        elif name == 'rate':
            try:
                _espeak.SetParameter(_espeak.RATE, value, 0)
            except ctypes.ArgumentError as e:
                raise ValueError(str(e))
        elif name == 'volume':
            try:
                _espeak.SetParameter(
                    _espeak.VOLUME, int(round(value * 100, 2)), 0)
            except TypeError as e:
                raise ValueError(str(e))
        elif name == 'pitch':
            try:
                _espeak.SetParameter(
                    _espeak.PITCH, int(value), 0
                )
            except TypeError as e:
                raise ValueError(str(e))
        else:
            raise KeyError('unknown property %s' % name)

    def save_to_file(self, text, filename):
        print("[EspeakDriver.save to file] synth to file.. ")
        code = self.numerise(filename)
        _espeak.Synth(toUtf8(text), flags=_espeak.ENDPAUSE | _espeak.CHARS_UTF8, user_data=code)

    def _start_synthesis(self, text):
        print(f"[EspeakDriver._start_synthesis] Starting synthesis for text: {text}")
        self._proxy.setBusy(True)
        self._proxy.notify('started-utterance')
        self._speaking = True
        self._data_buffer = b''  # Ensure buffer is cleared before starting
        try:
            print(f"[EspeakDriver._start_synthesis] Calling eSpeak with text: {text}")
            _espeak.Synth(toUtf8(text), flags=_espeak.ENDPAUSE | _espeak.CHARS_UTF8)
        except Exception as e:
            print(f"[EspeakDriver._start_synthesis] Error during speak: {e}")
            self._proxy.setBusy(False)
            self._proxy.notify('error', exception=e)
            raise
        finally:
            print("[EspeakDriver._start_synthesis] Finally block executed")


    def _onSynth(self, wav, numsamples, events):
        print("[EspeakDriver._onSynth] Called")
        i = 0
        while True:
            event = events[i]
            print(f"[EspeakDriver._onSynth] Event type: {event.type}, Position: {event.text_position}, Length: {event.length}")
            if event.type == _espeak.EVENT_LIST_TERMINATED:
                break
            if event.type == _espeak.EVENT_WORD:
                self._proxy.notify('started-word', location=event.text_position - 1, length=event.length)
            elif event.type == _espeak.EVENT_END:
                print("[EspeakDriver._onSynth] EVENT_END")
                stream = NamedTemporaryFile(delete=False, suffix='.wav')
    
                try:
                    with wave.open(stream, 'wb') as f:
                        f.setnchannels(1)
                        f.setsampwidth(2)
                        f.setframerate(22050.0)
                        f.writeframes(self._data_buffer)
                    self._data_buffer = b''
    
                    if event.user_data:
                        os.system(f'ffmpeg -y -i {stream.name} {self.decode_numeric(event.user_data)} -loglevel quiet')
                    else:
                        if platform.system() == 'Darwin':  # macOS
                            print(f"[EspeakDriver._onSynth] Playing sound on macOS... {stream.name}")
                            try:
                                result = subprocess.run(['afplay', stream.name], check=True, capture_output=True, text=True)
                                print(f"[EspeakDriver._onSynth] afplay result: {result.returncode}")
                                print(f"[EspeakDriver._onSynth] afplay stdout: {result.stdout}")
                                print(f"[EspeakDriver._onSynth] afplay stderr: {result.stderr}")
                                if result.returncode == 0:
                                    print(f"[EspeakDriver._onSynth] afplay completed successfully")
                                else:
                                    print(f"[EspeakDriver._onSynth] afplay failed with return code {result.returncode}")
                            except subprocess.CalledProcessError as e:
                                print(f"[EspeakDriver._onSynth] afplay failed with error: {e}")
                        elif platform.system() == 'Linux':
                            os.system(f'aplay {stream.name} -q')
                        elif platform.system() == 'Windows':
                            print(f"[EspeakDriver._onSynth] Playing sound on Windows... {stream.name}")
                            winsound.PlaySound(stream.name, winsound.SND_FILENAME)  # Blocking playback
                            print(f"[EspeakDriver._onSynth] Finished playing sound on Windows... {stream.name}")
    
                except Exception as e:
                    raise RuntimeError(f"Error during playback: {e}")
    
                finally:
                    try:
                        stream.close()  # Ensure the file is closed
                        os.remove(stream.name)
                    except Exception as e:
                        raise RuntimeError(f"Error deleting temporary WAV file: {e}")
    
                self._proxy.notify('finished-utterance', completed=True)
                self._proxy.setBusy(False)
                self.endLoop()  # End the loop here
                break  # Exit the loop after handling the termination event
    
            i += 1
    
        if numsamples > 0:
            self._data_buffer += ctypes.string_at(wav, numsamples * ctypes.sizeof(ctypes.c_short))
        return 0
    
    def endLoop(self):
        self._looping = False
    
    def startLoop(self):
        first = True
        self._looping = True
        while self._looping:
            if not self._looping:
                break
            if first:
                self._proxy.setBusy(False)
                first = False
                if self._text_to_say:
                    self._start_synthesis(self._text_to_say)
            self.iterate()
            time.sleep(0.01)
    
    def iterate(self):
        if not self._looping:
            return
        if self._stopping:
            _espeak.Cancel()
            self._stopping = False
            self._proxy.notify('finished-utterance', completed=False)
            self._proxy.setBusy(False)
            self.endLoop()



    def say(self, text):
        self._text_to_say = text
