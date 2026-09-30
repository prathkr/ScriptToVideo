import json
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional
from zipfile import ZIP_DEFLATED, ZipFile

import streamlit as st
import yaml
from streamlit_local_storage import LocalStorage

from placeholder_utils import extract_placeholders, missing_placeholders, replace_placeholders
from tts_utils import ElevenLabsError, generate_elevenlabs_audio
from video_utils import (
    adjust_audio_speed,
    append_outro,
    cleanup_old_generated_files,
    get_video_duration,
    loop_video_to_duration,
    make_temp_output,
    merge_video_audio,
    remove_silence,
)

st.set_page_config(page_title="Script -> Video Batch Generator", layout="wide")

local_storage = LocalStorage()
STORAGE_KEY = "eleven_profiles"
MAX_SCRIPTS = 5
OUTPUT_RETENTION_SECONDS = 30 * 60


def load_profiles() -> dict:
    raw = local_storage.getItem(STORAGE_KEY)
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def save_profiles(profiles: dict) -> None:
    local_storage.setItem(STORAGE_KEY, json.dumps(profiles), key="ls_set_profiles")
    time.sleep(0.5)


def ensure_state() -> None:
    defaults = {
        "scripts": [],
        "generated_files": [],
        "zip_bytes": None,
        "zip_created_at": None,
        "placeholder_values": {},
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def clear_generated_download() -> None:
    st.session_state.zip_bytes = None
    st.session_state.zip_created_at = None
    st.session_state.generated_files = []


def clear_script_widget_state() -> None:
    for key in list(st.session_state.keys()):
        if key.startswith("script_") or key.startswith("delete_"):
            del st.session_state[key]


def build_zip_bytes(files: List[str]) -> bytes:
    temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    temp_zip.close()
    try:
        with ZipFile(temp_zip.name, "w", compression=ZIP_DEFLATED) as zf:
            for file_path in files:
                zf.write(file_path, arcname=Path(file_path).name)
        return Path(temp_zip.name).read_bytes()
    finally:
        try:
            os.remove(temp_zip.name)
        except OSError:
            pass


def render_sidebar() -> dict:
    st.sidebar.header("ElevenLabs profile")

    profiles = load_profiles()
    NEW = "➕ New profile"
    choice = st.sidebar.selectbox("Profile", [*profiles.keys(), NEW])

    eleven_key = ""
    voice_id = ""

    if choice == NEW:
        name = st.sidebar.text_input("Profile name", key="new_profile_name")
        new_key = st.sidebar.text_input(
            "ElevenLabs API key", type="password", key="new_profile_key"
        )
        new_voice = st.sidebar.text_input(
            "ElevenLabs voice ID", key="new_profile_voice"
        )

        if st.sidebar.button("Save profile"):
            if name.strip() and new_key.strip() and new_voice.strip():
                profiles[name.strip()] = {
                    "key": new_key.strip(),
                    "voice_id": new_voice.strip(),
                }
                save_profiles(profiles)
                st.rerun()
            else:
                st.sidebar.error("Name, key and voice ID are all required.")

        eleven_key = new_key.strip()
        voice_id = new_voice.strip()
    else:
        eleven_key = profiles[choice].get("key", "")
        voice_id = profiles[choice].get("voice_id", "")
        st.sidebar.success(f"Using profile: {choice}")
        st.sidebar.caption(f"Voice ID: {voice_id}")

        if st.sidebar.button("Delete this profile"):
            profiles.pop(choice, None)
            save_profiles(profiles)
            st.rerun()

    st.sidebar.caption(
        "Your key is stored only in your browser and sent to this app only to call "
        "ElevenLabs. It is never saved on the server."
    )

    st.sidebar.header("Timing")
    duration_mode = st.sidebar.radio(
        "How should narration length and b-roll length be matched?",
        options=["loop_broll", "speed_narration"],
        format_func=lambda v: {
            "loop_broll": "Loop the b-roll to match narration (recommended)",
            "speed_narration": "Fit narration into a fixed b-roll length (may speed up voice)",
        }[v],
    )
    remove_silence_flag = st.sidebar.checkbox(
        "Remove long silences from narration", value=True
    )

    return {
        "eleven_key": eleven_key,
        "voice_id": voice_id,
        "duration_mode": duration_mode,
        "remove_silence": remove_silence_flag,
    }


def render_scripts_editor() -> None:
    st.header("1. Scripts")
    st.caption(
        "Use {placeholder} tags anywhere in a script, e.g. {company}, {process}. "
        f"Max {MAX_SCRIPTS} scripts per batch."
    )

    uploaded_yaml = st.file_uploader(
        "Load scripts from a YAML file (list of strings)",
        type=["yaml", "yml"],
    )
    if uploaded_yaml is not None:
        try:
            yaml_data = yaml.safe_load(uploaded_yaml.read().decode("utf-8"))
            if isinstance(yaml_data, list):
                if st.button("Load scripts from file"):
                    st.session_state.scripts = [str(item) for item in yaml_data]
                    clear_generated_download()
                    clear_script_widget_state()
                    st.rerun()
            else:
                st.warning("YAML must be a list of scripts.")
        except Exception as e:
            st.error(f"Could not load YAML: {e}")

    scripts_to_delete = None
    for i in range(len(st.session_state.scripts)):
        col1, col2 = st.columns([10, 1])
        with col1:
            st.session_state.scripts[i] = st.text_area(
                f"Script {i + 1}",
                value=st.session_state.scripts[i],
                key=f"script_{i}",
                height=120,
            )
        with col2:
            st.write("")
            st.write("")
            if st.button("🗑️", key=f"delete_{i}"):
                scripts_to_delete = i

    if scripts_to_delete is not None:
        st.session_state.scripts.pop(scripts_to_delete)
        clear_script_widget_state()
        clear_generated_download()
        st.rerun()

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Add script"):
            st.session_state.scripts.append("")
            clear_generated_download()
            st.rerun()

    with col2:
        if st.button("Delete all scripts"):
            st.session_state.scripts = []
            clear_script_widget_state()
            clear_generated_download()
            st.rerun()


def render_broll_uploaders() -> List[Optional[object]]:
    st.header("3. B-roll for each video")
    st.caption(
        "Upload a different b-roll clip for each script. "
        "The b-roll's original audio is removed and replaced by the ElevenLabs narration."
    )

    uploads: List[Optional[object]] = []
    for i, script in enumerate(st.session_state.scripts):
        if not script.strip():
            uploads.append(None)
            continue

        upload = st.file_uploader(
            f"B-roll for Script {i + 1}",
            type=["mp4", "mov", "m4v"],
            key=f"broll_{i}",
        )
        uploads.append(upload)

    return uploads


def render_placeholder_inputs(placeholders: List[str]) -> Dict[str, str]:
    st.header("2. Placeholder values")
    values: Dict[str, str] = {}

    if not placeholders:
        st.info("No {placeholders} found yet — add some to your scripts above.")
        return values

    cols = st.columns(2)
    for i, placeholder in enumerate(placeholders):
        state_key = f"placeholder_{placeholder}"
        if state_key not in st.session_state.placeholder_values:
            st.session_state.placeholder_values[state_key] = ""

        with cols[i % 2]:
            values[placeholder] = st.text_input(
                placeholder.replace("_", " ").title(),
                key=state_key,
            )

    return values


def generate_videos(
    scripts: List[str],
    placeholder_values: Dict[str, str],
    broll_uploads: List[Optional[object]],
    outro_bytes: Optional[bytes],
    settings: dict,
    status_box,
) -> List[str]:
    temp_paths: List[str] = []
    final_files: List[str] = []
    success = False

    outro_path = None
    if outro_bytes:
        outro_path = make_temp_output(".mp4")
        Path(outro_path).write_bytes(outro_bytes)
        temp_paths.append(outro_path)

    try:
        for idx, raw_script in enumerate(scripts):
            if not raw_script.strip():
                continue

            label = f"Script {idx + 1}"

            if idx >= len(broll_uploads) or broll_uploads[idx] is None:
                st.error(f"{label}: no b-roll uploaded — skipped.")
                continue

            status_box.write(f"**{label}**: checking placeholders...")

            missing = missing_placeholders(raw_script, placeholder_values)
            if missing:
                st.error(
                    f"{label}: missing values for "
                    f"{', '.join('{' + m + '}' for m in missing)} — skipped."
                )
                continue

            script_text = replace_placeholders(raw_script, placeholder_values)

            broll_path = make_temp_output(".mp4")
            Path(broll_path).write_bytes(broll_uploads[idx].getvalue())
            temp_paths.append(broll_path)

            fixed_broll_duration = None
            if settings["duration_mode"] == "speed_narration":
                fixed_broll_duration = get_video_duration(broll_path)

            status_box.write(f"**{label}**: generating narration...")
            try:
                raw_audio = generate_elevenlabs_audio(
                    script_text,
                    settings["eleven_key"],
                    settings["voice_id"],
                )
            except ElevenLabsError as e:
                st.error(f"{label}: {e}")
                continue

            temp_paths.append(raw_audio)

            narration_audio = raw_audio
            if settings["remove_silence"]:
                narration_audio = remove_silence(raw_audio)
                if narration_audio not in temp_paths:
                    temp_paths.append(narration_audio)

            merged_video = make_temp_output(".mp4")
            temp_paths.append(merged_video)

            if settings["duration_mode"] == "loop_broll":
                status_box.write(
                    f"**{label}**: looping its b-roll to match narration..."
                )
                narration_duration = get_video_duration(narration_audio)

                looped_video = make_temp_output(".mp4")
                temp_paths.append(looped_video)

                loop_video_to_duration(
                    broll_path,
                    narration_duration,
                    looped_video,
                )
                merge_video_audio(
                    looped_video,
                    narration_audio,
                    merged_video,
                )
            else:
                status_box.write(
                    f"**{label}**: fitting narration to its b-roll length..."
                )
                final_audio = adjust_audio_speed(
                    narration_audio,
                    fixed_broll_duration,
                )
                if final_audio not in temp_paths:
                    temp_paths.append(final_audio)

                merge_video_audio(
                    broll_path,
                    final_audio,
                    merged_video,
                    copy_video=True,
                )

            if outro_path:
                status_box.write(f"**{label}**: appending outro...")
                with_outro = make_temp_output(".mp4")
                temp_paths.append(with_outro)
                append_outro(merged_video, outro_path, with_outro)
                merged_video = with_outro

            final_files.append(merged_video)
            status_box.write(f"**{label}**: ✅ done")

        success = True
        return final_files

    finally:
        to_delete = set(temp_paths)
        if success:
            to_delete -= set(final_files)
        else:
            to_delete |= set(final_files)

        for path in to_delete:
            try:
                os.remove(path)
            except OSError:
                pass


def main() -> None:
    ensure_state()

    # Clean generated ZIPs/files older than 30 minutes.
    cleanup_old_generated_files(OUTPUT_RETENTION_SECONDS)

    # Also expire this browser session's download after 30 minutes.
    created_at = st.session_state.zip_created_at
    if (
        st.session_state.zip_bytes is not None
        and created_at is not None
        and time.time() - created_at >= OUTPUT_RETENTION_SECONDS
    ):
        clear_generated_download()

    st.title("Script → Voice → B-roll video generator")

    settings = render_sidebar()

    st.header("Uploads")
    uploaded_outro = st.file_uploader(
        "Optional outro clip",
        type=["mp4", "mov", "m4v"],
    )

    render_scripts_editor()
    placeholders = extract_placeholders(st.session_state.scripts)
    placeholder_values = render_placeholder_inputs(placeholders)
    broll_uploads = render_broll_uploaders()

    st.header("4. Generate")

    if st.button("Generate all videos", type="primary"):
        # A new generation replaces the previous download.
        clear_generated_download()

        problems = []

        if not settings["eleven_key"] or not settings["voice_id"]:
            problems.append("ElevenLabs API key + voice ID are required.")

        non_empty = [s for s in st.session_state.scripts if s.strip()]
        if not non_empty:
            problems.append("Add at least one non-empty script.")

        if len(non_empty) > MAX_SCRIPTS:
            problems.append(f"Max {MAX_SCRIPTS} scripts per batch.")

        missing_broll = [
            str(i + 1)
            for i, script in enumerate(st.session_state.scripts)
            if script.strip()
            and (i >= len(broll_uploads) or broll_uploads[i] is None)
        ]

        if missing_broll:
            problems.append(
                "Upload b-roll for every non-empty script. "
                f"Missing: Script {', Script '.join(missing_broll)}."
            )

        if problems:
            for p in problems:
                st.error(p)
            st.stop()

        status_box = st.container()

        with st.spinner("Generating videos..."):
            try:
                final_files = generate_videos(
                    st.session_state.scripts,
                    placeholder_values,
                    broll_uploads,
                    uploaded_outro.read() if uploaded_outro else None,
                    settings,
                    status_box,
                )

                if final_files:
                    st.session_state.zip_bytes = build_zip_bytes(final_files)
                    st.session_state.zip_created_at = time.time()

                    for f in final_files:
                        try:
                            os.remove(f)
                        except OSError:
                            pass

                    st.success(
                        f"Generated {len(final_files)} video(s). "
                        "The download will expire after 30 minutes."
                    )
                else:
                    st.warning(
                        "No videos were generated — check the errors above."
                    )

            except Exception as e:
                st.error(f"Generation stopped: {e}")

    if st.session_state.zip_bytes:
        st.download_button(
            label="Download all videos as ZIP",
            data=st.session_state.zip_bytes,
            file_name="generated_videos.zip",
            mime="application/zip",
        )
        st.caption("This download is kept for 30 minutes after generation.")


if __name__ == "__main__":
    main()
