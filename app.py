import os
import time
import json
import asyncio
import subprocess
import uuid
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from nicegui import ui, app

from google.cloud import storage
from google.cloud import speech_v1
from google.cloud import speech_v2
from google.oauth2 import service_account

# Load environment variables
load_dotenv()

# Directories
TEMP_DIR = Path("temp")
RESULTS_DIR = Path("results")
TEMP_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

class AppState:
    def __init__(self):
        self.gcs_bucket = os.getenv("GCP_BUCKET_NAME", "")
        self.gcp_location = os.getenv("GCP_LOCATION", "us-central1")
        self.credentials_info = None
        
        self.audio_filename = None
        self.audio_content = None
        
        self.is_processing = False

        # Transcription Options
        self.api_version = "V2 (BatchRecognize)"
        self.model = "chirp_2"
        self.language_code = "en-US"
        self.profanity_filter = False
        self.enable_automatic_punctuation = False
        self.enable_spoken_punctuation = False
        self.enable_spoken_emojis = False
        self.speaker_diarization = False
        self.enable_word_time_offsets = True
        self.max_alternatives = 1

state = AppState()

# UI State variables for binding
ui_state = {
    'log': 'Welcome to the Long-Audio Speech-to-Text Transcriber.',
    'status': 'Idle'
}

def log(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    full_message = f"[{timestamp}] {message}"
    print(full_message)
    ui_state['log'] += f"\n{full_message}"
    
async def handle_cred_upload(e):
    try:
        content = await e.file.text()
        state.credentials_info = json.loads(content)
        log("✅ Credentials JSON loaded successfully.")
        ui.notify("Credentials loaded successfully!", type='positive')
    except Exception as ex:
        log(f"❌ Error loading credentials: {ex}")
        ui.notify("Invalid JSON credentials file.", type='negative')

async def handle_audio_upload(e):
    try:
        state.audio_filename = e.file.name
        state.audio_content = await e.file.read()
        log(f"✅ Audio file '{state.audio_filename}' loaded into memory ({len(state.audio_content)} bytes).")
        ui.notify(f"Audio '{state.audio_filename}' loaded!", type='positive')
    except Exception as ex:
        log(f"❌ Error loading audio file: {ex}")
        ui.notify("Failed to load audio file.", type='negative')

async def process_audio():
    if not state.credentials_info:
        ui.notify("Please upload your Service Account JSON credentials first.", type='warning')
        return
    if not state.audio_content:
        ui.notify("Please upload an MP3 audio file first.", type='warning')
        return
    if not state.gcs_bucket:
        ui.notify("Please enter a Google Cloud Storage bucket name.", type='warning')
        return
        
    state.is_processing = True
    ui_state['status'] = 'Processing...'
    btn_start.disable()
    
    try:
        log("Initializing GCP clients...")
        credentials = service_account.Credentials.from_service_account_info(state.credentials_info)
        storage_client = storage.Client(credentials=credentials)
        project_id = credentials.project_id
        
        client_options = None
        if state.gcp_location and state.gcp_location != "global":
            client_options = {"api_endpoint": f"{state.gcp_location}-speech.googleapis.com"}
        
        unique_id = str(uuid.uuid4())[:8]
        bucket = storage_client.bucket(state.gcs_bucket)
        full_transcript = []

        if state.api_version == "V1 (LongRunningRecognize)":
            # ---------------- V1 Logic (No chunking needed) ----------------
            gcs_filename = f"audio_{unique_id}_{state.audio_filename}"
            log(f"Uploading audio to gs://{state.gcs_bucket}/{gcs_filename} ...")
            blob = bucket.blob(gcs_filename)
            await asyncio.to_thread(blob.upload_from_string, state.audio_content)
            gcs_uri = f"gs://{state.gcs_bucket}/{gcs_filename}"
            log(f"✅ Uploaded to {gcs_uri}")

            speech_client = speech_v1.SpeechClient(credentials=credentials, client_options=client_options)
            log(f"Starting V1 LongRunningRecognize operation using {state.model} model in {state.gcp_location}...")
            
            diarization_config = speech_v1.SpeakerDiarizationConfig(
                enable_speaker_diarization=state.speaker_diarization,
            )

            config = speech_v1.RecognitionConfig(
                encoding=speech_v1.RecognitionConfig.AudioEncoding.MP3,
                language_code=state.language_code,
                model=state.model,
                profanity_filter=state.profanity_filter,
                enable_automatic_punctuation=state.enable_automatic_punctuation,
                enable_spoken_punctuation=state.enable_spoken_punctuation,
                enable_spoken_emojis=state.enable_spoken_emojis,
                enable_word_time_offsets=state.enable_word_time_offsets,
                max_alternatives=int(state.max_alternatives),
                diarization_config=diarization_config,
            )
            audio = speech_v1.RecognitionAudio(uri=gcs_uri)
            
            operation = await asyncio.to_thread(speech_client.long_running_recognize, config=config, audio=audio)
            log("Long running recognition operation started. Waiting for results (this may take several minutes)...")
            response = await asyncio.to_thread(operation.result, timeout=28800)
            log("✅ Recognition operation completed!")
            
            for result in response.results:
                if result.alternatives:
                    full_transcript.append(result.alternatives[0].transcript)

            log(f"Cleaning up {gcs_uri} from GCS...")
            await asyncio.to_thread(blob.delete)
            log("✅ Cleanup complete.")
                    
        else: 
            # ---------------- V2 Logic (With 15-min Chunking) ----------------
            log("Chunking audio file into 15-minute segments to bypass BatchRecognize limits...")
            input_path = TEMP_DIR / f"input_{unique_id}.mp3"
            with open(input_path, "wb") as f:
                f.write(state.audio_content)
                
            chunk_dir = TEMP_DIR / f"{unique_id}_chunks"
            chunk_dir.mkdir(exist_ok=True)
            chunk_pattern = chunk_dir / "chunk_%03d.mp3"
            
            cmd = ["ffmpeg", "-i", str(input_path), "-f", "segment", "-segment_time", "900", "-c", "copy", str(chunk_pattern)]
            def run_ffmpeg():
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await asyncio.to_thread(run_ffmpeg)
            
            chunks = sorted(chunk_dir.glob("chunk_*.mp3"))
            log(f"✅ Split audio into {len(chunks)} chunks.")
            
            gcs_uris = []
            uploaded_blobs = []
            log(f"Uploading {len(chunks)} chunks to GCS...")
            for chunk_file in chunks:
                chunk_blob_name = f"audio_{unique_id}_{chunk_file.name}"
                blob = bucket.blob(chunk_blob_name)
                await asyncio.to_thread(blob.upload_from_filename, str(chunk_file))
                gcs_uris.append(f"gs://{state.gcs_bucket}/{chunk_blob_name}")
                uploaded_blobs.append(blob)
                
            speech_client = speech_v2.SpeechClient(credentials=credentials, client_options=client_options)
            log(f"Starting V2 BatchRecognize operation using {state.model} model in {state.gcp_location}...")
            recognizer_path = f"projects/{project_id}/locations/{state.gcp_location}/recognizers/_"
            
            features = speech_v2.RecognitionFeatures(
                profanity_filter=state.profanity_filter,
                enable_automatic_punctuation=state.enable_automatic_punctuation,
                enable_spoken_punctuation=state.enable_spoken_punctuation,
                enable_spoken_emojis=state.enable_spoken_emojis,
                enable_word_time_offsets=state.enable_word_time_offsets,
                max_alternatives=int(state.max_alternatives),
            )

            if state.speaker_diarization:
                features.diarization_config = speech_v2.SpeakerDiarizationConfig()

            config = speech_v2.RecognitionConfig(
                auto_decoding_config=speech_v2.AutoDetectDecodingConfig(),
                model=state.model,
                language_codes=[state.language_code],
                features=features,
            )
            
            gcs_output_uri = f"gs://{state.gcs_bucket}/results/{unique_id}/"
            
            # BatchRecognize accepts max 15 files per request, so if there's more than 15 chunks, process them in batches of 15
            batch_size = 15
            for i in range(0, len(gcs_uris), batch_size):
                batch_uris = gcs_uris[i:i+batch_size]
                log(f"Processing batch of {len(batch_uris)} chunks...")
                
                request = speech_v2.BatchRecognizeRequest(
                    recognizer=recognizer_path,
                    config=config,
                    files=[speech_v2.BatchRecognizeFileMetadata(uri=uri) for uri in batch_uris],
                    recognition_output_config=speech_v2.RecognitionOutputConfig(
                        gcs_output_config=speech_v2.GcsOutputConfig(uri=gcs_output_uri)
                    )
                )
                
                operation = await asyncio.to_thread(speech_client.batch_recognize, request=request)
                log(f"Batch recognition operation started. Operation ID: {operation.operation.name}")
                
                try:
                    start_time = time.time()
                    last_log_time = start_time
                    while not await asyncio.to_thread(operation.done):
                        await asyncio.sleep(15) # Check status every 15 seconds
                        
                        elapsed = int(time.time() - start_time)
                        if time.time() - last_log_time >= 60: # Log every 1 minute
                            meta = operation.metadata
                            if meta:
                                try:
                                    progress = getattr(meta, 'progress_percent', 0)
                                    log(f"⏱️ Elapsed: {elapsed}s. GCP Overall Progress: {progress}%")
                                    
                                    brm = getattr(meta, 'batch_recognize_metadata', None)
                                    if brm and hasattr(brm, 'transcription_metadata'):
                                        count = 0
                                        for uri, fmeta in brm.transcription_metadata.items():
                                            chunk_prog = getattr(fmeta, 'progress_percent', 0)
                                            if chunk_prog > 0:
                                                log(f"   -> {uri.split('/')[-1]}: {chunk_prog}%")
                                                count += 1
                                        if count == 0 and progress == 0:
                                            log(f"   (Waiting for chunks to start processing...)")
                                except Exception as e:
                                    log(f"⏱️ Elapsed: {elapsed}s. Processing on GCP... (Could not parse progress: {e})")
                            else:
                                log(f"⏱️ Elapsed: {elapsed}s. No metadata received from GCP yet.")
                            last_log_time = time.time()
                            
                        if elapsed > 28800:
                            raise Exception("Operation timed out locally after 8 hours.")
                            
                    response = operation.result()
                    log(f"✅ Batch Recognition completed in {int(time.time() - start_time)} seconds!")
                except Exception as op_ex:
                    log(f"⚠️ Operation error or timeout: {op_ex}")
                    raise op_ex
                
                # We sort the results by filename to ensure correct transcript order
                sorted_results = sorted(response.results.items(), key=lambda x: x[0])
                
                for uri, result_file in sorted_results:
                    if result_file.error and result_file.error.code != 0:
                        log(f"⚠️ Error for {uri}: {result_file.error.message}")
                    
                    if result_file.cloud_storage_result and result_file.cloud_storage_result.uri:
                        result_uri = result_file.cloud_storage_result.uri
                        
                        prefix = f"gs://{state.gcs_bucket}/"
                        if result_uri.startswith(prefix):
                            blob_path = result_uri[len(prefix):]
                            result_blob = bucket.blob(blob_path)
                            
                            json_data = await asyncio.to_thread(result_blob.download_as_text)
                            batch_res = speech_v2.BatchRecognizeResults.from_json(json_data)
                            
                            for res in batch_res.results:
                                if res.alternatives:
                                    full_transcript.append(res.alternatives[0].transcript)
                            
                            await asyncio.to_thread(result_blob.delete)
                    
                    elif result_file.inline_result:
                        for res in result_file.inline_result.transcript.results:
                            if res.alternatives:
                                full_transcript.append(res.alternatives[0].transcript)

            log(f"Cleaning up {len(uploaded_blobs)} chunks from GCS...")
            for blob in uploaded_blobs:
                await asyncio.to_thread(blob.delete)
            
            # Clean up local chunks
            try:
                for chunk_file in chunks:
                    chunk_file.unlink()
                chunk_dir.rmdir()
                input_path.unlink()
            except Exception as e:
                log(f"⚠️ Could not remove local temp files: {e}")
                
            log("✅ Cleanup complete.")

        transcript_text = "\n".join(full_transcript)
        
        if not transcript_text.strip():
            log("⚠️ Transcript is empty. No speech was detected.")
            transcript_text = "(No speech detected)"
            
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        res_dir = RESULTS_DIR / timestamp_str
        res_dir.mkdir(exist_ok=True)
        out_filepath = res_dir / "transcript.txt"
        
        with open(out_filepath, "w", encoding="utf-8") as f:
            f.write(transcript_text)
            
        log(f"✅ Transcript saved locally to {out_filepath}")
        
        ui.notify(f"Transcription complete! Saved to {out_filepath}", type='positive', timeout=10000)
        
    except Exception as ex:
        log(f"❌ Error during processing: {ex}")
        ui.notify(f"An error occurred: {ex}", type='negative', timeout=10000)
    finally:
        state.is_processing = False
        ui_state['status'] = 'Idle'
        btn_start.enable()

@ui.page('/')
def index():
    ui.page_title('Speech-to-Text Transcriber')
    
    with ui.column().classes('w-full max-w-4xl mx-auto p-4 gap-6'):
        ui.label('Long-Audio Speech-to-Text Transcriber').classes('text-3xl font-bold text-center')
        
        with ui.card().classes('w-full'):
            ui.label('1. Configuration').classes('text-xl font-semibold mb-2')
            
            with ui.row().classes('w-full items-center gap-4'):
                ui.input('GCS Bucket Name', placeholder='my-bucket-name').bind_value(state, 'gcs_bucket').classes('flex-grow')
                
                locations = [
                    'us-central1', 'us', 'eu', 'global', 
                    'asia-northeast1', 'asia-south1', 'asia-southeast1', 
                    'europe-west2', 'europe-west3', 'europe-west4',
                    'northamerica-northeast1'
                ]
                ui.select(locations, label='GCP Location').bind_value(state, 'gcp_location').classes('w-48')
                
            ui.label('Upload Service Account JSON (Credentials):').classes('mt-4 font-medium')
            ui.upload(on_upload=handle_cred_upload, max_files=1).props('accept=".json"').classes('max-w-full')
            
        with ui.card().classes('w-full'):
            ui.label('2. Audio Input').classes('text-xl font-semibold mb-2')
            ui.label('Upload MP3 File (will be temporarily stored in GCS):').classes('font-medium')
            ui.upload(on_upload=handle_audio_upload, max_files=1).props('accept=".mp3,audio/mpeg"').classes('max-w-full')
            
        with ui.card().classes('w-full'):
            ui.label('3. Transcription Options').classes('text-xl font-semibold mb-2')
            
            with ui.row().classes('w-full gap-4'):
                api_versions = ['V1 (LongRunningRecognize)', 'V2 (BatchRecognize)']
                ui.select(api_versions, label='API Version').bind_value(state, 'api_version').classes('w-64')
                
                models = [
                    'chirp_2', 'chirp_3', 'chirp3', 'chirp', 'long', 'short', 
                    'telephony', 'latest_long', 'latest_short', 
                    'latest_telephony', 'default'
                ]
                ui.select(models, label='Model').bind_value(state, 'model').classes('flex-grow')
                ui.input('Language Code (e.g. en-US, es-ES)').bind_value(state, 'language_code').classes('w-32')
                ui.number('Max Alts', min=1, max=30, value=1).bind_value(state, 'max_alternatives').classes('w-24')

            with ui.row().classes('w-full gap-4 mt-2'):
                ui.checkbox('Profanity Filter').bind_value(state, 'profanity_filter')
                ui.checkbox('Automatic Punctuation').bind_value(state, 'enable_automatic_punctuation')
                ui.checkbox('Spoken Punctuation').bind_value(state, 'enable_spoken_punctuation')
                ui.checkbox('Spoken Emojis').bind_value(state, 'enable_spoken_emojis')
                
            with ui.row().classes('w-full gap-4 mt-2'):
                ui.checkbox('Speaker Diarization').bind_value(state, 'speaker_diarization')
                ui.checkbox('Word-Time Offsets').bind_value(state, 'enable_word_time_offsets')
            
        with ui.row().classes('w-full justify-center mt-4'):
            global btn_start
            btn_start = ui.button('Start Transcription', on_click=process_audio).classes('px-8 py-2 text-lg')
            btn_start.props('color="primary" icon="play_arrow"')
            
        with ui.card().classes('w-full mt-4 bg-gray-50'):
            ui.label('Status & Logs').classes('text-xl font-semibold mb-2')
            ui.label().bind_text_from(ui_state, 'status', backward=lambda s: f"Status: {s}").classes('font-bold mb-2')
            
            log_view = ui.log().classes('w-full h-64 font-mono text-sm bg-black text-green-400 p-2 rounded')
            
            def update_log():
                current_lines = ui_state['log'].split('\n')
                if not hasattr(log_view, '_last_line_count'):
                    log_view._last_line_count = 0
                    
                if len(current_lines) > log_view._last_line_count:
                    for line in current_lines[log_view._last_line_count:]:
                        if line:
                            log_view.push(line)
                    log_view._last_line_count = len(current_lines)
                    
            ui.timer(0.5, update_log)

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(title='Chirp Transcriber', port=8080, reload=False)
