# Comparison-only image: preserve the baseline Python, dependencies and OS.
ARG BASE_IMAGE=ghcr.io/eturkok/pliqra@sha256:ac0a44b919c5d469f90d2c1e331601c58780ad70e698e545dfd367914b7efa7f
FROM ${BASE_IMAGE}
ARG REVISION=working-tree
LABEL org.opencontainers.image.revision=$REVISION
COPY --chown=pliqra:pliqra pliqra /app/pliqra
