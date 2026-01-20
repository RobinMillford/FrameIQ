# FrameIQ - Complete Google Cloud Run Deployment Guide

## 📋 Project Information

- **Project Name**: MovieTvHub
- **Service Name**: frameiq
- **Region**: asia-south1
- **Live URL**: https://frameiq-344233295407.asia-south1.run.app/
- **GitHub Repository**: https://github.com/RobinMillford/FrameIQ

---

## 🎯 Table of Contents

1. [Initial GCP Setup](#initial-gcp-setup)
2. [Manual Deployment](#manual-deployment)
3. [CI/CD Setup with GitHub Actions](#cicd-setup-with-github-actions)
4. [Environment Variables Configuration](#environment-variables-configuration)
5. [Useful Commands](#useful-commands)
6. [Troubleshooting](#troubleshooting)

---

## 🚀 Initial GCP Setup

### Step 1: Install Google Cloud CLI

```bash
# Install gcloud CLI (choose one method)

# Method 1: Using snap (Ubuntu/Debian)
sudo snap install google-cloud-cli

# Method 2: Official installer
curl https://sdk.cloud.google.com | bash
exec -l $SHELL

# Verify installation
gcloud --version
```

### Step 2: Authenticate and Configure Project

```bash
# Login to Google Cloud
gcloud auth login

# Set your project
gcloud config set project movietvhub

# Verify project is set
gcloud config get-value project

# Set default region
gcloud config set run/region asia-south1
```

### Step 3: Enable Required APIs

```bash
# Enable Cloud Run API
gcloud services enable run.googleapis.com

# Enable Container Registry API
gcloud services enable containerregistry.googleapis.com

# Enable Artifact Registry API
gcloud services enable artifactregistry.googleapis.com
```

---

## 📦 Manual Deployment

### Method 1: Direct Cloud Run Deployment (Easiest)

This method builds and deploys in one command:

```bash
# Navigate to project directory
cd /home/robin/Downloads/FrameIQ

# Deploy to Cloud Run (builds automatically)
gcloud run deploy frameiq \
  --source . \
  --region asia-south1 \
  --platform managed \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --timeout 300 \
  --max-instances 10 \
  --min-instances 0 \
  --port 8080
```

**What this does:**

- Builds Docker image from your Dockerfile
- Pushes image to Google Container Registry
- Deploys to Cloud Run
- Makes service publicly accessible

### Method 2: Build Docker Image First (More Control)

#### Step 1: Build and Push Docker Image

```bash
# Set variables
PROJECT_ID=movietvhub
SERVICE_NAME=frameiq
REGION=asia-south1

# Build Docker image
docker build -t gcr.io/$PROJECT_ID/$SERVICE_NAME:latest .

# Push to Google Container Registry
docker push gcr.io/$PROJECT_ID/$SERVICE_NAME:latest
```

#### Step 2: Deploy to Cloud Run

```bash
gcloud run deploy frameiq \
  --image gcr.io/$PROJECT_ID/$SERVICE_NAME:latest \
  --region asia-south1 \
  --platform managed \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --timeout 300 \
  --max-instances 10 \
  --min-instances 0 \
  --port 8080
```

---

## 🔄 CI/CD Setup with GitHub Actions

### Overview

This setup enables automatic deployment to Cloud Run whenever you push code to GitHub's `main` or `master` branch.

### Step 1: Create Service Account

```bash
# Set project ID
PROJECT_ID=movietvhub

# Create service account for GitHub Actions
gcloud iam service-accounts create github-actions \
  --display-name="GitHub Actions Deploy" \
  --project=$PROJECT_ID

# Get service account email
SA_EMAIL=github-actions@${PROJECT_ID}.iam.gserviceaccount.com
echo "Service Account: $SA_EMAIL"
```

### Step 2: Grant IAM Permissions

```bash
# Grant Cloud Run Admin role
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.admin"

# Grant Storage Admin role
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.admin"

# Grant Service Account User role
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser"

# Grant Artifact Registry Writer role (for pushing Docker images)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/artifactregistry.writer"
```

### Step 3: Create Service Account Key

```bash
# Create and download key
gcloud iam service-accounts keys create github-actions-key.json \
  --iam-account=${SA_EMAIL}

# Add to .gitignore (IMPORTANT!)
echo "github-actions-key.json" >> .gitignore

# View the key (you'll need to copy this)
cat github-actions-key.json
```

> **⚠️ SECURITY WARNING**: Never commit `github-actions-key.json` to Git! It's already added to `.gitignore`.

### Step 4: Create GitHub Actions Workflow

The workflow file is already created at `.github/workflows/deploy-to-cloud-run.yml`:

```yaml
name: Deploy to Cloud Run

on:
  push:
    branches:
      - main
      - master
  workflow_dispatch:

env:
  PROJECT_ID: movietvhub
  SERVICE_NAME: frameiq
  REGION: asia-south1

jobs:
  deploy:
    name: Deploy to Cloud Run
    runs-on: ubuntu-latest

    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Authenticate to Google Cloud
        uses: google-github-actions/auth@v2
        with:
          credentials_json: ${{ secrets.GCP_SA_KEY }}

      - name: Set up Cloud SDK
        uses: google-github-actions/setup-gcloud@v2

      - name: Configure Docker for GCR
        run: gcloud auth configure-docker

      - name: Build and Push Docker Image
        run: |
          docker build -t gcr.io/${{ env.PROJECT_ID }}/${{ env.SERVICE_NAME }}:${{ github.sha }} .
          docker build -t gcr.io/${{ env.PROJECT_ID }}/${{ env.SERVICE_NAME }}:latest .
          docker push gcr.io/${{ env.PROJECT_ID }}/${{ env.SERVICE_NAME }}:${{ github.sha }}
          docker push gcr.io/${{ env.PROJECT_ID }}/${{ env.SERVICE_NAME }}:latest

      - name: Deploy to Cloud Run
        run: |
          gcloud run deploy ${{ env.SERVICE_NAME }} \
            --image gcr.io/${{ env.PROJECT_ID }}/${{ env.SERVICE_NAME }}:${{ github.sha }} \
            --region ${{ env.REGION }} \
            --platform managed \
            --allow-unauthenticated \
            --memory 1Gi \
            --cpu 1 \
            --timeout 300 \
            --max-instances 10 \
            --min-instances 0 \
            --port 8080 \
            --set-env-vars GROQ_API_KEY=${{ secrets.GROQ_API_KEY }},TMDB_API_KEY=${{ secrets.TMDB_API_KEY }},TMDB_API_KEY_2=${{ secrets.TMDB_API_KEY_2 }},CHROMA_API_KEY=${{ secrets.CHROMA_API_KEY }},CHROMA_TENANT=${{ secrets.CHROMA_TENANT }},CHROMA_DATABASE=${{ secrets.CHROMA_DATABASE }},SECRET_KEY=${{ secrets.SECRET_KEY }},GOOGLE_CLIENT_ID=${{ secrets.GOOGLE_CLIENT_ID }},GOOGLE_CLIENT_SECRET=${{ secrets.GOOGLE_CLIENT_SECRET }},DATABASE_URL=${{ secrets.DATABASE_URL }},CLOUDINARY_CLOUD_NAME=${{ secrets.CLOUDINARY_CLOUD_NAME }},CLOUDINARY_API_KEY=${{ secrets.CLOUDINARY_API_KEY }},CLOUDINARY_API_SECRET=${{ secrets.CLOUDINARY_API_SECRET }},NEWS_API_KEY=${{ secrets.NEWS_API_KEY }}

      - name: Show Deployment URL
        run: |
          echo "🚀 Deployment successful!"
          echo "Service URL: https://frameiq-344233295407.asia-south1.run.app"
```

### Step 5: Add Secrets to GitHub

1. **Go to GitHub Repository**: https://github.com/RobinMillford/FrameIQ

2. **Navigate to**: Settings → Secrets and variables → Actions

3. **Click**: "New repository secret"

4. **Add these 15 secrets** (one by one):

| Secret Name             | How to Get the Value                                                 | Description                        |
| ----------------------- | -------------------------------------------------------------------- | ---------------------------------- |
| `GCP_SA_KEY`            | Contents of `github-actions-key.json` file (created in Step 3)       | GCP service account authentication |
| `GROQ_API_KEY`          | Get from https://console.groq.com/keys                               | Groq AI API for LLM functionality  |
| `TMDB_API_KEY`          | Get from https://www.themoviedb.org/settings/api                     | The Movie Database API (primary)   |
| `TMDB_API_KEY_2`        | Get from https://www.themoviedb.org/settings/api (optional)          | The Movie Database API (backup)    |
| `CHROMA_API_KEY`        | Get from https://www.trychroma.com                                   | Chroma vector database API key     |
| `CHROMA_TENANT`         | Provided by Chroma Cloud                                             | Chroma tenant identifier           |
| `CHROMA_DATABASE`       | Your database name in Chroma                                         | Chroma database name               |
| `SECRET_KEY`            | Generate: `python -c 'import secrets; print(secrets.token_hex(32))'` | Flask secret key for sessions      |
| `GOOGLE_CLIENT_ID`      | Get from Google Cloud Console → APIs & Services → Credentials        | Google OAuth 2.0 Client ID         |
| `GOOGLE_CLIENT_SECRET`  | Get from Google Cloud Console → APIs & Services → Credentials        | Google OAuth 2.0 Client Secret     |
| `DATABASE_URL`          | Get from Neon/Supabase/your PostgreSQL provider                      | PostgreSQL connection string       |
| `CLOUDINARY_CLOUD_NAME` | Get from https://cloudinary.com/console                              | Cloudinary cloud name              |
| `CLOUDINARY_API_KEY`    | Get from https://cloudinary.com/console                              | Cloudinary API key                 |
| `CLOUDINARY_API_SECRET` | Get from https://cloudinary.com/console                              | Cloudinary API secret              |
| `NEWS_API_KEY`          | Get from https://newsapi.org/register                                | News API key                       |

> **📝 Note**: Keep all these values in your `.env` file locally and as GitHub Secrets. Never commit them to your repository!

### Step 6: Trigger First Deployment

```bash
# Add and commit the workflow file
git add .github/workflows/deploy-to-cloud-run.yml .gitignore
git commit -m "Add GitHub Actions CI/CD for automatic Cloud Run deployment"

# Push to GitHub (this triggers the deployment!)
git push origin main
```

### Step 7: Monitor Deployment

1. Go to: https://github.com/RobinMillford/FrameIQ/actions
2. Click on the latest workflow run
3. Watch the deployment progress in real-time
4. Deployment takes ~7-9 minutes

---

## 🔐 Environment Variables Configuration

### Set Environment Variables Manually

If you need to update environment variables on Cloud Run:

```bash
gcloud run services update frameiq \
  --region asia-south1 \
  --update-env-vars \
GROQ_API_KEY=your_groq_api_key,\
TMDB_API_KEY=your_tmdb_api_key,\
TMDB_API_KEY_2=your_tmdb_api_key_2,\
CHROMA_API_KEY=your_chroma_api_key,\
CHROMA_TENANT=your_chroma_tenant,\
CHROMA_DATABASE=your_chroma_database,\
SECRET_KEY=your_flask_secret_key,\
GOOGLE_CLIENT_ID=your_google_client_id,\
GOOGLE_CLIENT_SECRET=your_google_client_secret,\
DATABASE_URL=your_postgresql_url,\
CLOUDINARY_CLOUD_NAME=your_cloudinary_name,\
CLOUDINARY_API_KEY=your_cloudinary_key,\
CLOUDINARY_API_SECRET=your_cloudinary_secret,\
NEWS_API_KEY=your_news_api_key
```

### View Current Environment Variables

```bash
gcloud run services describe frameiq \
  --region asia-south1 \
  --format='value(spec.template.spec.containers[0].env)'
```

---

## 📊 Useful Commands

### View Service Details

```bash
# Get service information
gcloud run services describe frameiq --region asia-south1

# Get service URL
gcloud run services describe frameiq --region asia-south1 --format='value(status.url)'
```

### View Logs

```bash
# Stream logs in real-time
gcloud run services logs tail frameiq --region asia-south1

# View recent logs
gcloud run services logs read frameiq --region asia-south1 --limit 50
```

### Update Service Configuration

```bash
# Update memory
gcloud run services update frameiq --region asia-south1 --memory 2Gi

# Update CPU
gcloud run services update frameiq --region asia-south1 --cpu 2

# Update max instances
gcloud run services update frameiq --region asia-south1 --max-instances 20

# Update min instances (for faster cold starts)
gcloud run services update frameiq --region asia-south1 --min-instances 1
```

### Rollback to Previous Version

```bash
# List revisions
gcloud run revisions list --service frameiq --region asia-south1

# Rollback to specific revision
gcloud run services update-traffic frameiq \
  --region asia-south1 \
  --to-revisions REVISION_NAME=100
```

### Delete Service

```bash
# Delete the Cloud Run service
gcloud run services delete frameiq --region asia-south1
```

---

## 🔄 How CI/CD Works

### Deployment Flow

```
1. Developer pushes code to GitHub (main/master branch)
   ↓
2. GitHub Actions workflow triggers automatically
   ↓
3. Checkout code from repository
   ↓
4. Authenticate with GCP using service account key
   ↓
5. Build Docker image from Dockerfile
   ↓
6. Push image to Google Container Registry
   ↓
7. Deploy to Cloud Run with environment variables
   ↓
8. Service is live at: https://frameiq-344233295407.asia-south1.run.app
```

### Deployment Timeline

- **Build Docker Image**: ~6-7 minutes
- **Push to Registry**: ~30 seconds
- **Deploy to Cloud Run**: ~2 minutes
- **Total**: ~9 minutes from push to live

### Manual Trigger

You can also trigger deployment manually:

1. Go to: https://github.com/RobinMillford/FrameIQ/actions
2. Select "Deploy to Cloud Run" workflow
3. Click "Run workflow"
4. Select branch and click "Run workflow"

---

## 🐛 Troubleshooting

### Build Fails

```bash
# Check build logs
gcloud builds list --limit 5

# View specific build log
gcloud builds log BUILD_ID
```

### Service Won't Start

```bash
# Check service logs
gcloud run services logs read frameiq --region asia-south1 --limit 100

# Check if environment variables are set
gcloud run services describe frameiq --region asia-south1 --format='value(spec.template.spec.containers[0].env)'
```

### Permission Denied Errors

If you see "Permission denied" errors during CI/CD:

```bash
# Ensure service account has all required roles
gcloud projects get-iam-policy movietvhub \
  --flatten="bindings[].members" \
  --filter="bindings.members:github-actions@movietvhub.iam.gserviceaccount.com"
```

### Port Issues

Make sure your `Dockerfile` exposes port 8080 and your app listens on `$PORT`:

```dockerfile
EXPOSE 8080
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 120 app:app
```

### GitHub Actions Fails

1. **Check secrets are set**: Go to Settings → Secrets and variables → Actions
2. **Verify GCP_SA_KEY**: Make sure it contains the full JSON content
3. **Check logs**: View the failed workflow run for detailed error messages

---

## 💰 Cost Optimization

Cloud Run pricing is based on:

- **CPU**: Only charged when processing requests
- **Memory**: Only charged when processing requests
- **Requests**: $0.40 per million requests

### Tips to Reduce Costs:

1. **Set min-instances to 0** (default) - No charges when idle
2. **Use appropriate memory** - 1Gi is usually sufficient
3. **Set max-instances** - Prevent runaway costs (set to 10)
4. **Enable request timeout** - Prevent long-running requests (300s)
5. **Monitor usage**: https://console.cloud.google.com/run

---

## 📝 Quick Reference

### Essential Commands

```bash
# Deploy manually
gcloud run deploy frameiq --source . --region asia-south1

# View logs
gcloud run services logs tail frameiq --region asia-south1

# Update environment variables
gcloud run services update frameiq --region asia-south1 --update-env-vars KEY=VALUE

# View service URL
gcloud run services describe frameiq --region asia-south1 --format='value(status.url)'
```

### Important Links

- **Live App**: https://frameiq-344233295407.asia-south1.run.app
- **GitHub Repo**: https://github.com/RobinMillford/FrameIQ
- **GitHub Actions**: https://github.com/RobinMillford/FrameIQ/actions
- **GCP Console**: https://console.cloud.google.com/run
- **Cloud Run Docs**: https://cloud.google.com/run/docs
- **Pricing**: https://cloud.google.com/run/pricing

---

## ✅ Success Checklist

- [x] GCP CLI installed and configured
- [x] Project created (movietvhub)
- [x] APIs enabled (Cloud Run, Container Registry, Artifact Registry)
- [x] Dockerfile created
- [x] Manual deployment successful
- [x] Service account created for GitHub Actions
- [x] IAM roles granted
- [x] Service account key generated
- [x] GitHub secrets configured (15 secrets)
- [x] GitHub Actions workflow created
- [x] CI/CD pipeline tested and working
- [x] Environment variables set on Cloud Run
- [x] App is live and accessible

---

## 🎉 You're All Set!

Your FrameIQ app is now:

- ✅ Deployed to Google Cloud Run
- ✅ Automatically deploys on every push to GitHub
- ✅ Secured with proper IAM roles
- ✅ Environment variables configured
- ✅ Scalable and cost-effective

**Every time you push code to GitHub, it will automatically deploy to production in ~9 minutes!** 🚀
