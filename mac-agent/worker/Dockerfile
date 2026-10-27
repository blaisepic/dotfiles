FROM node:22-bookworm-slim
ARG CODEX_VERSION=0.163.0
ARG WORKER_UID=10001
ARG WORKER_GID=10001
RUN apt-get update && apt-get install -y --no-install-recommends git gh python3 ca-certificates tini && rm -rf /var/lib/apt/lists/* \
 && npm install --global @openai/codex@${CODEX_VERSION} \
 && (getent group ${WORKER_GID} >/dev/null || groupadd --gid ${WORKER_GID} worker) \
 && (test "${WORKER_UID}" != 0 || (echo 'WORKER_UID must be nonzero' >&2; exit 1)) \
 && useradd --create-home --uid ${WORKER_UID} --gid ${WORKER_GID} --shell /bin/sh worker \
 && mkdir -p /run/mac-agent /workspace /home/worker/.codex/sessions /home/worker/state \
 && chown -R ${WORKER_UID}:${WORKER_GID} /home/worker /workspace
COPY --chmod=0755 launch.py /usr/local/lib/mac-agent/launch.py
COPY --chmod=0755 entrypoint /usr/local/bin/mac-agent-entrypoint
COPY --chmod=0755 git-credential-mac-agent /usr/local/bin/git-credential-mac-agent
COPY --chmod=0755 gh /usr/local/bin/gh
USER ${WORKER_UID}:${WORKER_GID}
ENV HOME=/home/worker CODEX_HOME=/home/worker/.codex GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/home/worker/.gitconfig
ENTRYPOINT ["/usr/bin/tini","--","/usr/local/bin/mac-agent-entrypoint"]
