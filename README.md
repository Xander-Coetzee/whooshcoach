# WhooshCoach 🚴‍♂️🤖

**WhooshCoach** is a self-hosted, autonomous AI cycling coach and interactive training calendar. It connects directly to your virtual cycling account (MyWhoosh), monitors completed ride fatigue and training stress (TSS), periodizes your weekly training around goal races (A/B/C priority), and prescribes workouts from the MyWhoosh catalog using Google Gemini.

---

## 🌟 Key Features

* **Autonomous AI Periodization**: Powered by Google Gemini (`gemini-3.6-flash`). Dynamically adjusts weekly duration and TSS based on real-time fatigue, recent rides, and target race proximity.
* **Direct MyWhoosh Cloud Sync**: Automatically pulls completed activities, duration, power, and heart rate directly from MyWhoosh—zero manual `.fit` file dropping required.
* **Interactive Training Calendar**: Built with FullCalendar. Displays workouts by training zones (Z1 Recovery through Z6 Anaerobic), race targets, and rest days.
* **Race & Target Event Tracking**: Add target races (A/B/C priority, distance, discipline). The AI Coach tapers training volume before A-races and schedules post-race recovery.
* **Protected Athlete Commitments**: Manually created rest days, custom outdoor rides, and target races are strictly protected from AI overwrites.
* **Dynamic Post-Ride Adaptations**: If a ride significantly exceeds planned TSS, the AI Coach automatically adjusts upcoming sessions to prevent overtraining.
* **Self-Hosted Docker Deployment**: Runs with Docker Compose on local servers (e.g. Unraid, Debian, Ubuntu).

---

## 🚀 Quick Start

### 1. Clone & Configure
```bash
git clone https://github.com/Xander-Coetzee/whooshcoach.git
cd whooshcoach
cp .env.example .env
```

Set your Gemini API Key in `.env` (or configure it directly in the Web UI Settings):
```env
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-3.6-flash
```

### 2. Run with Docker Compose
```bash
docker compose up -d --build
```
Access the dashboard in your browser at `http://localhost:8555`.

---

## 🛠️ Tech Stack

* **Backend**: Python 3.12, FastAPI, SQLAlchemy, SQLite, APScheduler, HTTPX
* **AI Engine**: Google GenAI SDK (`google-genai`), Gemini 3.6 Flash / Flash-Lite fallback
* **Frontend**: Vanilla JavaScript (ES6+), Tailwind CSS, FullCalendar, Lucide Icons
* **Deployment**: Docker, Docker Compose

---

## 📜 License
MIT License.
