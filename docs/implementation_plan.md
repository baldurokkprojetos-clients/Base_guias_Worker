# Implementation Plan - Base Guias Unimed

## Goal Description
Build a complete system for managing "Guias Unimed" scraping jobs, including a Database, Backend API, Worker system for distributed scraping, and a Frontend Dashboard.

The system will:
1.  Manage `Carteirinhas` (beneficiary cards) and `BaseGuias` (scraped data).
2.  Distribute scraping `Jobs` across multiple "servers" (worker instances).
3.  Provide a Dashboard for monitoring, uploading data, and exporting results.

## User Review Required
> [!IMPORTANT]
> **Database**: I will create a `migrations` folder with SQL files. I assume PostgreSQL (Supabase) based on `.env`.
> **Worker Architecture**: The existing script `ImportBaseGuias.py` will be refactored. I will wrap it in a lightweight HTTP server (FastAPI) to act as the "Server" listening on ports 8000-8005, which the Dispatcher will call.
> **Deployment**: The "servers" mentioned in `.env` (ports 8000-8005) will be simulated locally or setup as separate processes.

## Proposed Changes

### Database
#### [NEW] `backend/migrations`
- `0001_initial_schema.sql`:
    - `users`: `id`, `user` (name), `api_key`, `status` (Ativo/Inativo), `validade`, `created_at`, `updated_at`.
    - `carteirinhas`: `id`, `carteirinha` (string), `paciente` (string), `created_at`, `updated_at`.
    - `jobs`: `id`, `carteirinha_id`, `status` (success, pending, processing, error), `attempts`, `priority`, `locked_by` (server url), `timeout`, `created_at`, `updated_at`.
    - `base_guias`: `id`, `carteirinha_id`, `guia`, `data_autorizacao`, `senha`, `validade`, `codigo_terapia`, `nome_terapia`, `sessoes_autorizadas`, `created_at`, `updated_at`.
- `0002_seed_data.sql`:
    - Insert default user "Clinica Larissa Martins Ferreira" with generated API Key.

### Backend (API)
Directory: `backend/`
- **Framework**: FastAPI.
- **Files**:
    - `main.py`: Entry point.
    - `database.py`: DB connection.
    - `models.py`: Pydantic models.
    - `routes/carteirinhas.py`: Upload (Excel), CRUD.
    - `routes/jobs.py`: Create jobs (Single/Batch), Status.
    - `routes/guias.py`: Export (Excel/JSON) with date filters.

### Worker & Scraper
Directory: `Worker/`
- **Refactoring**:
    - `ImportBaseGuias.py`: Modify to be a class or function that accepts a single `carteirinha` and returns data/status, instead of iterating a file.
    - `server.py` (New): A small FastAPI app that runs on ports 8000-8005. It has an endpoint `POST /process_job` that calls `ImportBaseGuias`.
    - `dispatcher.py` (New): The main "Worker". Runs every 15s.
        - Checks DB for `pending` jobs.
        - Checks availability of Servers (healthcheck).
        - Assigns job -> Updates DB (`processing`, `locked_by`).
        - Calls Server `POST /process_job`.

### Frontend
Directory: `frontend/`
- **Framework**: React + Vite.
- **Styling**: CSS Modules / Vanilla CSS (Futuristic Dark Theme).
- **Pages**:
    - **Leayout**: Sidebar, Branding Footer.
    - **Dashboard**:
        - Stats (Success/Error).
        - Quick Actions (New Request).
        - Patient Search.
    - **Carteirinhas**:
        - List view.
        - Upload Modal (Excel).
    - **Guia List**:
        - Table with filters (Date, Patient).
        - Export Button.
    - **Logs**:
        - Job history.

## Verification Plan

### Automated Tests
- **API Tests**: Use `requests` or `pytest` to hit endpoints:
    - Upload dummy excel.
    - Create job.
    - Check job status.
- **Worker Simulation**:
    - Start 1 Server instance.
    - Run Dispatcher.
    - Create a Job.
    - Verify Job Status changes to `processing` -> `success/error`.

### Manual Verification
1.  **Database**: Check tables created in Supabase (or local DB).
2.  **Frontend**:
    - Open Dashboard.
    - Upload `carteirinhas.xlsx`.
    - Trigger "Processar Todos".
    - Watch "Logs" for updates.
    - Verify "Base Guias" populated.
    - Download Excel Export.
