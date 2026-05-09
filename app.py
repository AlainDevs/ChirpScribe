import os
import time
import json
import asyncio
import uuid
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from nicegui import ui, app

from google.cloud import storage
from google.cloud.speech_v2 import (
    SpeechClient, 
    AutoDetectDecodingConfig, 
    RecognitionConfig, 
    RecognitionFeatures, 
    BatchRecognizeRequest, 
    BatchRecognizeFileMetadata, 
    RecognitionOutputConfig, 
    InlineOutputConfig
)
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

state = AppState()

# UI State variables for binding
ui_state = {
    'log': 'Welcome to the Long-Audio Speech-to-Text Transcriber (Chirp V2).',
    'status': 'Idle'
}

def log(message: str):
    timestamp = datetime.now().strftime("%H:%M:%S")
    full_message = f"[{timestamp}] {message}"
    print(full_message)
    ui_state['log'] += f"\n{full_message}"
    
    # We update the log element in the UI via the bound element if possible,
    # or rely on ui.timer/bind_text to refresh.

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
        # 1. Initialize GCP clients
        log("Initializing GCP clients...")
        credentials = service_account.Credentials.from_service_account_info(state.credentials_info)
        storage_client = storage.Client(credentials=credentials)
        speech_client = SpeechClient(credentials=credentials)
        project_id = credentials.project_id
        
        # 2. Upload to GCS
        unique_id = str(uuid.uuid4())[:8]
        gcs_filename = f"audio_{unique_id}_{state.audio_filename}"
        log(f"Uploading audio to gs://{state.gcs_bucket}/{gcs_filename} ...")
        
        bucket = storage_client.bucket(state.gcs_bucket)
        blob = bucket.blob(gcs_filename)
        # Uploading synchronously might block the UI, but we run in an async context,
        # ideally we should run blocking in an executor. For simplicity, we just upload.
        await asyncio.to_thread(blob.upload_from_string, state.audio_content)
        gcs_uri = f"gs://{state.gcs_bucket}/{gcs_filename}"
        log(f"✅ Uploaded to {gcs_uri}")
        
        # 3. Trigger BatchRecognize (V2)
        log(f"Starting V2 BatchRecognize operation using Chirp model in {state.gcp_location}...")
        recognizer_path = f"projects/{project_id}/locations/{state.gcp_location}/recognizers/_"
        
        config = RecognitionConfig(
            auto_decoding_config=AutoDetectDecodingConfig(),
            model="chirp",
            language_codes=["en-US"], # Chirp supports multi-language, but en-US is standard default
            features=RecognitionFeatures(
                enable_word_time_offsets=True,
            ),
        )
        
        request = BatchRecognizeRequest(
            recognizer=recognizer_path,
            config=config,
            files=[BatchRecognizeFileMetadata(uri=gcs_uri)],
            recognition_output_config=RecognitionOutputConfig(
                inline_response_config=InlineOutputConfig()
            )
        )
        
        operation = await asyncio.to_thread(speech_client.batch_recognize, request=request)
        log("Batch recognition operation started. Waiting for results (this may take several minutes)...")
        
        # 4. Wait for operation
        response = await asyncio.to_thread(operation.result, timeout=7200) # Up to 2 hours
        log("✅ Recognition operation completed!")
        
        # 5. Process results
        full_transcript = []
        for uri, result_file in response.results.items():
            if result_file.error and result_file.error.code != 0:
                log(f"⚠️ Error for {uri}: {result_file.error.message}")
            if result_file.inline_result:
                for result in result_file.inline_result.transcript.results:
                    if result.alternatives:
                        full_transcript.append(result.alternatives[0].transcript)
                        
        transcript_text = "\n".join(full_transcript)
        
        if not transcript_text.strip():
            log("⚠️ Transcript is empty. No speech was detected.")
            transcript_text = "(No speech detected)"
            
        # 6. Save locally
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        res_dir = RESULTS_DIR / timestamp_str
        res_dir.mkdir(exist_ok=True)
        out_filepath = res_dir / "transcript.txt"
        
        with open(out_filepath, "w", encoding="utf-8") as f:
            f.write(transcript_text)
            
        log(f"✅ Transcript saved locally to {out_filepath}")
        
        # 7. Cleanup GCS (Optional, but implemented)
        log(f"Cleaning up {gcs_uri} from GCS...")
        await asyncio.to_thread(blob.delete)
        log("✅ Cleanup complete.")
        
        ui.notify(f"Transcription complete! Saved to {out_filepath}", type='positive', timeout=10000)
        
    except Exception as ex:
        log(f"❌ Error during processing: {ex}")
        ui.notify(f"An error occurred: {ex}", type='negative', timeout=10000)
    finally:
        state.is_processing = False
        ui_state['status'] = 'Idle'
        btn_start.enable()

# Build the UI
@ui.page('/')
def index():
    ui.page_title('Chirp V2 Transcriber')
    
    with ui.column().classes('w-full max-w-4xl mx-auto p-4 gap-6'):
        ui.label('Long-Audio Speech-to-Text Transcriber').classes('text-3xl font-bold text-center')
        
        with ui.card().classes('w-full'):
            ui.label('1. Configuration').classes('text-xl font-semibold mb-2')
            
            with ui.row().classes('w-full items-center gap-4'):
                ui.input('GCS Bucket Name', placeholder='my-bucket-name').bind_value(state, 'gcs_bucket').classes('flex-grow')
                ui.input('GCP Location', placeholder='us-central1').bind_value(state, 'gcp_location').classes('w-32')
                
            ui.label('Upload Service Account JSON (Credentials):').classes('mt-4 font-medium')
            ui.upload(on_upload=handle_cred_upload, max_files=1).props('accept=".json"').classes('max-w-full')
            
        with ui.card().classes('w-full'):
            ui.label('2. Audio Input').classes('text-xl font-semibold mb-2')
            ui.label('Upload MP3 File (will be temporarily stored in GCS):').classes('font-medium')
            ui.upload(on_upload=handle_audio_upload, max_files=1).props('accept=".mp3,audio/mpeg"').classes('max-w-full')
            
        with ui.row().classes('w-full justify-center mt-4'):
            global btn_start
            btn_start = ui.button('Start Transcription', on_click=process_audio).classes('px-8 py-2 text-lg')
            btn_start.props('color="primary" icon="play_arrow"')
            
        with ui.card().classes('w-full mt-4 bg-gray-50'):
            ui.label('Status & Logs').classes('text-xl font-semibold mb-2')
            ui.label().bind_text_from(ui_state, 'status', backward=lambda s: f"Status: {s}").classes('font-bold mb-2')
            
            log_view = ui.log().classes('w-full h-64 font-mono text-sm bg-black text-green-400 p-2 rounded')
            # Custom bind for log, since ui.log is a bit different. We'll just push to it in a timer.
            
            def update_log():
                # Split log state by newline and only add new lines
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
