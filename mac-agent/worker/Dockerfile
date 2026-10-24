FROM node:22-bookworm-slim
ARG CODEX_VERSION=0.163.0
RUN apt-get update && apt-get install -y --no-install-recommends git gh python3 ca-certificates tini && rm -rf /var/lib/apt/lists/* \
 && npm install --global @openai/codex@${CODEX_VERSION} \
 && useradd --create-home --uid 10001 --shell /bin/sh worker \
 && mkdir -p /run/mac-agent /workspace /home/worker/.codex \
 && chown -R worker:worker /home/worker /workspace
COPY --chmod=0755 entrypoint /usr/local/bin/mac-agent-entrypoint
COPY --chmod=0755 git-credential-mac-agent /usr/local/bin/git-credential-mac-agent
COPY --chmod=0755 gh /usr/local/bin/gh-wrapper
USER 10001:10001
ENV HOME=/home/worker CODEX_HOME=/home/worker/.codex GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/home/worker/.gitconfig
ENTRYPOINT ["/usr/bin/tini","--","/usr/local/bin/mac-agent-entrypoint"]
