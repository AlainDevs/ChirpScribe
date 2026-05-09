# ChirpScribe

A web-based tool built with [NiceGUI](https://nicegui.io/) to transcribe long MP3 audio files (over 20 minutes) using the Google Cloud Speech-to-Text V2 API and the highly accurate **Chirp** model.

Because Google Cloud enforces strict file size and duration limits on synchronous requests, this app seamlessly circumvents the 20-minute barrier by:
1. Temporarily uploading your audio file to a Google Cloud Storage (GCS) bucket.
2. Triggering an asynchronous "Long-Running" Batch Recognition request.
3. Awaiting the results, saving them locally, and automatically cleaning up the temporary GCS audio file.

---

## 🚀 Installation & Setup

### 1. Clone & Set Up the Environment
Make sure you have Python 3.8 or newer installed on your machine.

```bash
# Create a Python virtual environment
python3 -m venv venv

# Activate the virtual environment
# On macOS/Linux:
source venv/bin/activate
# On Windows:
venv\Scripts\activate

# Install the required dependencies
pip install -r requirements.txt
```

### 2. Configure Environment (Optional)
You can create a `.env` file to store your default Google Cloud Storage bucket name and region, so you don't have to type it into the UI every time.
```bash
cp .env.example .env
```
Open `.env` and edit `GCP_BUCKET_NAME` to match your Google Cloud Storage bucket name.

---

## ☁️ Google Cloud Setup (Getting Your Key)

To use this app, you need a Google Cloud Project with the Speech-to-Text API enabled, a Storage Bucket, and a Service Account JSON Key.

### Step 1: Create a Project & Enable APIs
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project (or select an existing one).
3. Search for **Cloud Speech-to-Text API** in the top search bar and click **Enable**.

### Step 2: Create a Cloud Storage Bucket
1. In the Google Cloud Console menu, navigate to **Cloud Storage > Buckets**.
2. Click **Create** to make a new bucket.
3. Choose a unique name (e.g., `my-speech-transcripts-bucket`). 
4. Leave the other settings as default and create the bucket. **Take note of the bucket name.**

### Step 3: Create a Service Account & JSON Key
This key is what the application uses to securely upload files and trigger the transcription on your behalf.
1. Navigate to **IAM & Admin > Service Accounts**.
2. Click **Create Service Account**. Give it a name (e.g., `speech-transcriber`) and click **Create and Continue**.
3. **Grant the following Roles:**
   - `Cloud Speech Administrator` (Allows it to use the Speech-to-Text API)
   - `Storage Object Admin` (Allows it to upload and delete files in your bucket)
4. Click **Done**.
5. Find the newly created Service Account in the list, click the three dots (`⋮`) on the right, and select **Manage keys**.
6. Click **Add Key > Create new key**, choose **JSON**, and click **Create**.
7. The JSON file will automatically download to your computer. Keep this file secure!

---

## 🖥️ How to Use the App

1. Ensure your virtual environment is active (`source venv/bin/activate`).
2. Run the application:
   ```bash
   python app.py
   ```
3. Open your web browser and navigate to: **`http://localhost:8080`**
4. In the Web UI:
   - **GCS Bucket Name**: Enter the name of the bucket you created in Step 2.
   - **Service Account JSON**: Upload the JSON key file you downloaded in Step 3.
   - **Audio Input**: Upload the `.mp3` file you wish to transcribe.
5. Click **Start Transcription**.
6. Monitor the progress in the status log window. Transcribing long audio can take several minutes to hours depending on the length of the file.

### Accessing Results
Once completed, the application will notify you. Your transcription text will be saved locally inside the `results/` folder, organized by the date and time the process was completed.

```text
results/
└── 20260509_132030/
    └── transcript.txt
```
*(The temporary MP3 file uploaded to your bucket will be automatically deleted to save you storage space.)*