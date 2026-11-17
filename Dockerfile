# Flotilla in a container, for running inside a VPN container's network (README, "Behind gluetun").
# Built by .github/workflows/release.yml from the release's own wheel, so the image and the wheel
# are the same code, and every dependency is fixed at build time: a container starts the same way
# every time, without reaching PyPI or GitHub first.
FROM python:3.13-slim

ARG WHEEL
COPY dist/${WHEEL} /tmp/
RUN pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore /tmp/${WHEEL} \
 && rm /tmp/${WHEEL}

# Settings, state and seeded files all live under /root: mount a folder or volume there.
ENV HOME=/root
WORKDIR /root
ENTRYPOINT ["flotilla"]
CMD ["run"]

LABEL org.opencontainers.image.source="https://github.com/nickmontag/flotilla-face" \
      org.opencontainers.image.description="Flotilla Face: seed the rarest pieces of at-risk open-weight models" \
      org.opencontainers.image.licenses="MIT"
