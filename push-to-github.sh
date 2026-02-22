#!/bin/bash
# Run this after installing Git. Pushes this project to GitHub.
set -e
cd "$(dirname "$0")"

if ! command -v git &>/dev/null; then
  echo "Git is not installed. Install it with:"
  echo "  sudo apt install git"
  exit 1
fi

if [ ! -d .git ]; then
  git init
  git add .
  git commit -m "Initial commit: media-downloader"
  git branch -M main
  git remote add origin https://github.com/spsharath1522/video-downloader.git
  echo "Repository initialized. Push with:"
  echo "  git push -u origin main"
  echo ""
  echo "If GitHub asks for auth, use a Personal Access Token as password."
  exit 0
fi

# Already a repo: just add, commit if needed, and push
git add .
if [ -n "$(git status --porcelain)" ]; then
  git commit -m "Update media-downloader"
fi
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/spsharath1522/video-downloader.git
git push -u origin main
