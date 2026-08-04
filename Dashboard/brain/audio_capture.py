import numpy as np 
import sounddevice as sd

SAMPLE_RATE = 16000
SILENCE_THRESHOLD = 0.01
SILENCE_DURATION = 1
CHUNK_DURATION = 0.1

def record_until_silence(max_duration: float = 15.0)-> np.ndarray:
    chunk_samples = int(CHUNK_DURATION*SAMPLE_RATE)
    silence_chunks_needed = int(SILENCE_DURATION/CHUNK_DURATION)
    buffer, silent_chunks,speaking_started = [],0,False
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
        total_samples, max_samples = 0, int(max_duration * SAMPLE_RATE)
        while total_samples < max_samples:
            chunk, _ = stream.read(chunk_samples)
            chunk = chunk.flatten()
            buffer.append(chunk)
            total_samples += len(chunk)

            rms = np.sqrt(np.mean(chunk ** 2))
            if rms > SILENCE_THRESHOLD:
                speaking_started = True
                silent_chunks = 0
            elif speaking_started:
                silent_chunks += 1
                if silent_chunks >= silence_chunks_needed:
                    break

    return np.concatenate(buffer)


if __name__ == "__main__":
    from stt import transcribe_audio
    print("Listening...")
    audio = record_until_silence()
    # Pass the array itself: raw PCM bytes have no container header for FFmpeg
    # to parse, but faster-whisper takes float32 samples directly.
    print(transcribe_audio(audio))