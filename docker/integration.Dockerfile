# syntax=docker/dockerfile:1
# An integration's production image: the framework base image + one integration package.
#
#   docker build --target base -t framework-sanic-base .
#   docker build -f docker/integration.Dockerfile \
#       --build-arg BASE_IMAGE=framework-sanic-base --build-arg INTEGRATION=integrations/hubspot \
#       -t integration-hubspot .
#
# Once the framework is published, an integration's own repository keeps this file as its
# Dockerfile (INTEGRATION=.) and pins BASE_IMAGE to a framework release.

ARG BASE_IMAGE=framework-sanic-base:latest
FROM ${BASE_IMAGE}
ARG INTEGRATION=integrations/hubspot
USER root
COPY ${INTEGRATION} /tmp/integration
# --no-sources: the framework is already installed in the base image (no workspace here).
RUN uv pip install --python /app/.venv/bin/python --no-sources --no-cache /tmp/integration \
    && rm -rf /tmp/integration
USER app
