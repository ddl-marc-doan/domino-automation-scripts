# Dockerfile instructions only: Domino adds the FROM line from the base image
# (environment.json "baseImage", or --base-image).
USER root
RUN apt-get update && apt-get install -y --no-install-recommends graphviz \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir tabulate==0.9.0
USER ubuntu
