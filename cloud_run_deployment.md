# FrameIQ - Google Cloud Run Deployment Guide

## 📋 Project Information

- **Project Name**: MovieTvHub
- **Service Name**: frameiq
- **Region**: asia-south1
- **Live URL**: https://frameiq-344233295407.asia-south1.run.app/

## 🔧 Prerequisites

### 1. Install Google Cloud CLI

```bash
# Install gcloud CLI
sudo snap install google-cloud-cli

# Or use the official installer
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
```

### 2. Authenticate and Set Project

```bash
# Login to Google Cloud
gcloud auth login

# Set your project
gcloud config set project movietv hub

# Verify project is set
gcloud config get-value project
```

### 3. Enable Required APIs

```bash
# Enable Cloud Run API
gcloud services enable run.googleapis.com

# Enable Container Registry API
gcloud services enable containerregistry.googleapis.com

# Enable Artifact Registry API (recommended over Container Registry)
gcloud services enable artifactregistry.googleapis.com
```

## 🚀 Deployment Steps

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

### Method 2: Build Docker Image First (More Control)

#### Step 1: Build and Push Docker Image

```bash
# Set variables
PROJECT_ID=$(gcloud config get-value project)
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

## 🔐 Setting Environment Variables

Your app needs these environment variables to work:

```bash
gcloud run services update frameiq \
  --region asia-south1 \
  --update-env-vars \
GROQ_API_KEY=your_groq_api_key,\
TMDB_API_KEY=your_tmdb_api_key,\
CHROMA_API_KEY=your_chroma_api_key,\
CHROMA_TENANT=your_chroma_tenant,\
CHROMA_DATABASE=your_chroma_database,\
SECRET_KEY=your_flask_secret_key,\
GOOGLE_CLIENT_ID=your_google_client_id,\
GOOGLE_CLIENT_SECRET=your_google_client_secret,\
DATABASE_URL=your_postgresql_url,\
CLOUDINARY_CLOUD_NAME=your_cloudinary_name,\
CLOUDINARY_API_KEY=your_cloudinary_key,\
CLOUDINARY_API_SECRET=your_cloudinary_secret
```

> **Note**: Replace all `your_*` values with your actual credentials

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

## 🔄 Quick Redeploy Script

Create a file `deploy.sh` in your project root:

```bash
#!/bin/bash

# Quick deploy script for FrameIQ to Cloud Run

echo "🚀 Deploying FrameIQ to Google Cloud Run..."

# Set project
gcloud config set project movietvhub

# Deploy
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

echo "✅ Deployment complete!"
echo "🌐 Your app is live at: https://frameiq-344233295407.asia-south1.run.app/"
```

Make it executable:

```bash
chmod +x deploy.sh
./deploy.sh
```

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

### Port Issues

Make sure your [Dockerfile](file:///home/robin/Downloads/FrameIQ/Dockerfile) exposes port 8080 and your app listens on `$PORT`:

```dockerfile
EXPOSE 8080
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 120 app:app
```

## 💰 Cost Optimization

Cloud Run pricing is based on:
- **CPU**: Only charged when processing requests
- **Memory**: Only charged when processing requests  
- **Requests**: $0.40 per million requests

### Tips to Reduce Costs:

1. **Set min-instances to 0** (default) - No charges when idle
2. **Use appropriate memory** - 1Gi is usually sufficient
3. **Set max-instances** - Prevent runaway costs
4. **Enable request timeout** - Prevent long-running requests

## 📝 Notes

- Cloud Run automatically handles HTTPS
- Auto-scaling based on traffic
- No server management required
- Pay only for what you use
- Built-in load balancing

## 🔗 Useful Links

- [Cloud Run Documentation](https://cloud.google.com/run/docs)
- [Cloud Run Pricing](https://cloud.google.com/run/pricing)
- [Your Live App](https://frameiq-344233295407.asia-south1.run.app/)
