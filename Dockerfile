# cinemind in a container: BitTorrent (libtorrent), FFmpeg, the AI extras and the MCP server.
#
#   docker run --rm -it -v cinemind:/cinemind -v ./library:/library \
#       ghcr.io/extrudiostudio/cinemind get "Night of the Living Dead 1968" --yes
#
# Alpine, because libtorrent publishes wheels for musl Linux only. Settings, history, cache and
# logs live in /cinemind (CINEMIND_HOME); the TV and movie libraries default to /library.
# There is no system keyring: pass secrets as CINEMIND_<NAME> (CINEMIND_PLEX, ...).

ARG PYTHON=3.13

FROM python:${PYTHON}-alpine AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system \
    UV_PROJECT_ENVIRONMENT=/opt/cinemind
WORKDIR /src
# The dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-default-groups --all-extras --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-default-groups --all-extras --no-editable

FROM python:${PYTHON}-alpine
RUN apk add --no-cache ffmpeg tini \
    && adduser -D -u 1000 -h /home/cinemind cinemind \
    && mkdir -p /cinemind /library \
    && chown cinemind:cinemind /cinemind /library
COPY --from=build /opt/cinemind /opt/cinemind
ENV PATH=/opt/cinemind/bin:$PATH \
    CINEMIND_HOME=/cinemind \
    XDG_VIDEOS_DIR=/library \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    PYTHONUNBUFFERED=1
USER cinemind
WORKDIR /home/cinemind
VOLUME ["/cinemind", "/library"]
# BitTorrent's listening port (listen_interfaces); publish it for incoming peers.
EXPOSE 6881/tcp 6881/udp
ENTRYPOINT ["/sbin/tini", "--", "cinemind"]
CMD ["--help"]

LABEL org.opencontainers.image.title="cinemind" \
      org.opencontainers.image.description="An agent-operable media pipeline: find, fetch, check and file movies and TV for Plex" \
      org.opencontainers.image.source="https://github.com/ExtrudioStudio/cinemind" \
      org.opencontainers.image.licenses="MIT"
