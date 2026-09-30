import os
import subprocess
import tempfile
import time

from pydub import AudioSegment
from pydub.effects import speedup
from pydub.silence import split_on_silence


def make_temp_output(ext: str = ".mp4") -> str:
    temp = tempfile.NamedTemporaryFile(delete=False, prefix="script2video_", suffix=ext)
    temp.close()
    return temp.name


def clean_file_name(
    file_path: str,
    suffix: str = "_cleaned",
    new_ext: str = ".wav",
) -> str:
    base, _ = os.path.splitext(file_path)
    return f"{base}{suffix}{new_ext}"


def _run_ffmpeg(cmd: list[str]) -> None:
    """Run ffmpeg and expose a concise, useful error if it fails."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # Keep only the useful tail and avoid dumping a huge server log.
        tail = stderr[-600:] if stderr else "no ffmpeg error output"
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}): {tail}"
        )


def _run_ffprobe(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def get_video_duration(path: str) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    result = _run_ffprobe(cmd)

    if not result.stdout.strip():
        raise RuntimeError(
            result.stderr.strip() or f"Could not read duration of {path}"
        )

    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(
            f"Could not parse video duration for {path}"
        ) from exc


def _has_audio_stream(path: str) -> bool:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        path,
    ]
    result = _run_ffprobe(cmd)
    return bool(result.stdout.strip())


def remove_silence(audio_path: str, keep_silence_ms: int = 80) -> str:
    """Strip long silences out of narration audio. Returns a new file path."""
    sound = AudioSegment.from_file(audio_path)

    # Avoid a silence threshold based on -inf for an effectively silent file.
    if sound.dBFS == float("-inf"):
        return audio_path

    chunks = split_on_silence(
        sound,
        min_silence_len=350,
        silence_thresh=sound.dBFS - 16,
        keep_silence=keep_silence_ms,
    )

    if not chunks:
        return audio_path

    combined = AudioSegment.empty()
    for chunk in chunks:
        combined += chunk

    output_path = clean_file_name(
        audio_path,
        suffix="_nosilence",
        new_ext=".wav",
    )
    combined.export(output_path, format="wav")
    return output_path


def adjust_audio_speed(
    audio_path: str,
    target_duration_sec: float,
) -> str:
    """Speed narration up if longer than the fixed b-roll clip. Never slows it."""
    audio = AudioSegment.from_file(audio_path)
    current_duration = len(audio) / 1000.0

    if current_duration <= target_duration_sec or target_duration_sec <= 0:
        return audio_path

    playback_speed = current_duration / target_duration_sec
    sped_audio = speedup(audio, playback_speed=playback_speed)

    output_path = clean_file_name(
        audio_path,
        suffix="_sped",
        new_ext=".wav",
    )
    sped_audio.export(output_path, format="wav")
    return output_path


def loop_video_to_duration(
    video_path: str,
    target_duration_sec: float,
    output_path: str,
) -> None:
    """Loop/trim b-roll to exactly target_duration_sec with no original audio."""
    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-stream_loop",
        "-1",
        "-i",
        video_path,
        "-t",
        str(target_duration_sec),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        output_path,
    ]
    _run_ffmpeg(cmd)


def merge_video_audio(
    video_path: str,
    audio_path: str,
    output_path: str,
    copy_video: bool = False,
) -> None:
    """Mux video with narration. Original b-roll audio is discarded."""
    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy" if copy_video else "libx264",
        "-c:a",
        "aac",
        "-shortest",
        output_path,
    ]
    _run_ffmpeg(cmd)


def append_outro(
    main_video: str,
    outro_video: str,
    output_path: str,
) -> None:
    """Concatenate main video + outro, adding silent audio where necessary."""
    main_has_audio = _has_audio_stream(main_video)
    outro_has_audio = _has_audio_stream(outro_video)

    filter_parts: list[str] = []

    if main_has_audio:
        main_audio_pad = "[0:a]"
    else:
        main_dur = get_video_duration(main_video)
        filter_parts.append(
            f"aevalsrc=0:c=stereo:s=44100:d={main_dur}[main_sil]"
        )
        main_audio_pad = "[main_sil]"

    if outro_has_audio:
        outro_audio_pad = "[1:a]"
    else:
        outro_dur = get_video_duration(outro_video)
        filter_parts.append(
            f"aevalsrc=0:c=stereo:s=44100:d={outro_dur}[outro_sil]"
        )
        outro_audio_pad = "[outro_sil]"

    filter_parts.append(
        f"[0:v]{main_audio_pad}[1:v]{outro_audio_pad}"
        f"concat=n=2:v=1:a=1[vout][aout]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        main_video,
        "-i",
        outro_video,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        output_path,
    ]
    _run_ffmpeg(cmd)


def cleanup_old_generated_files(max_age_seconds: int = 1800) -> None:
    """Remove old app-generated temporary files.

    Streamlit Community Cloud may restart/reuse the process, so cleanup is
    intentionally based on file age rather than only session state.
    """
    temp_dir = tempfile.gettempdir()
    now = time.time()

    # Only touch files that this app deliberately marks as generated.
    prefixes = ("script2video_",)

    try:
        for filename in os.listdir(temp_dir):
            if not filename.startswith(prefixes):
                continue

            path = os.path.join(temp_dir, filename)

            try:
                if (
                    os.path.isfile(path)
                    and now - os.path.getmtime(path) >= max_age_seconds
                ):
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass
