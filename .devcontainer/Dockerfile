FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm

# The base image ships the Yarn apt repository, whose signing key it doesn't trust
# (NO_PUBKEY 62D54FD4003F6525). That makes `apt-get update` fail, so features that
# install packages (docker-in-docker, azure-cli) abort and Codespaces falls back to a
# recovery container. Yarn isn't used here, so drop the repository.
RUN rm -f /etc/apt/sources.list.d/yarn.list
