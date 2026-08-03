# syntax=docker/dockerfile:1.7
ARG CONTROLLER_IMAGE=docker.io/library/python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
ARG MICROMAMBA_IMAGE=docker.io/mambaorg/micromamba:2.8.1-ubuntu22.04@sha256:3ced392aa9520650382dc1c649386063022ae421389ec67ad0c4f96263408dc9
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c

FROM ${MICROMAMBA_IMAGE} AS micromamba
FROM ${UV_IMAGE} AS uv
FROM ${CONTROLLER_IMAGE} AS runtime

ARG SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.title="moseq2-test legacy worker" \
      org.opencontainers.image.description="Locked Python 3.7 MoSeq2 regression oracle with a Python 3.12 controller" \
      org.opencontainers.image.source="https://github.com/dattalab/moseq2-test" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}" \
      org.opencontainers.image.licenses="LicenseRef-MoSeq-NonCommercial-Research"

COPY --from=micromamba /bin/micromamba /usr/local/bin/micromamba
COPY --from=uv /uv /uvx /usr/local/bin/
COPY build/worker-inputs /tmp/worker-inputs

ENV MAMBA_ROOT_PREFIX=/opt/micromamba \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_PYTHON_VERSION_WARNING=1 \
    PYTHONNOUSERSITE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/moseq2/controller \
    UV_LINK_MODE=copy \
    MOSEQ2_TEST_SOURCE_ARCHIVE_MIRROR=/opt/moseq2/source_archives \
    MOSEQ2_TEST_SDIST_MIRROR=/opt/moseq2/sdists \
    MOSEQ2_TEST_WHEEL_MIRROR=/opt/moseq2/wheels \
    MOSEQ2_TEST_EXTERNAL_SOURCE_MIRROR=/opt/moseq2/external_sources \
    MOSEQ2_TEST_TEST_TOOL_WHEEL_MIRROR=/opt/moseq2/test_tool_wheels \
    MOSEQ2_TEST_BUILD_TOOLCHAIN_PREFIX=/opt/moseq2/build-toolchain \
    PATH=/opt/moseq2/controller/bin:/opt/moseq2/legacy/bin:/usr/local/bin:/usr/bin:/bin

RUN micromamba create --yes --offline --prefix /opt/moseq2/legacy \
      --file /tmp/worker-inputs/conda-local.explicit.txt \
    && micromamba create --yes --offline --prefix /opt/moseq2/build-toolchain \
      --file /tmp/worker-inputs/build-toolchain-local.explicit.txt \
    && /opt/moseq2/legacy/bin/python -m pip install --no-index --no-deps --require-hashes \
      --requirement /tmp/worker-inputs/pip-local.requirements.txt \
    && /opt/moseq2/legacy/bin/python -m pip install --no-index --no-deps --require-hashes \
      --requirement /tmp/worker-inputs/baseline-wheels-local.requirements.txt \
    && /opt/moseq2/legacy/bin/python -m pip check \
    && /opt/moseq2/legacy/bin/git --version \
    && /opt/moseq2/build-toolchain/bin/x86_64-conda-linux-gnu-cc --version \
    && /opt/moseq2/build-toolchain/bin/x86_64-conda-linux-gnu-c++ --version \
    && test -f /opt/moseq2/build-toolchain/include/crypt.h \
    && mkdir -p /opt/moseq2/source_archives /opt/moseq2/sdists /opt/moseq2/wheels \
      /opt/moseq2/external_sources /opt/moseq2/test_tool_wheels /usr/share/moseq2-test \
    && cp -a /tmp/worker-inputs/source_archives/. /opt/moseq2/source_archives/ \
    && cp -a /tmp/worker-inputs/sdists/. /opt/moseq2/sdists/ \
    && cp -a /tmp/worker-inputs/wheels/. /opt/moseq2/wheels/ \
    && cp -a /tmp/worker-inputs/external_sources/. /opt/moseq2/external_sources/ \
    && cp -a /tmp/worker-inputs/test_tool_wheels/. /opt/moseq2/test_tool_wheels/ \
    && cp -a /tmp/worker-inputs/metadata/. /usr/share/moseq2-test/ \
    && micromamba clean --all --yes

COPY pyproject.toml uv.lock README.md LICENSE.md NOTICE.md /opt/moseq2/framework-source/
COPY src /opt/moseq2/framework-source/src
COPY environments /opt/moseq2/framework-source/environments
COPY licenses /opt/moseq2/framework-source/licenses
COPY manifests /opt/moseq2/framework-source/manifests
COPY profiles /opt/moseq2/framework-source/profiles
COPY recipes /opt/moseq2/framework-source/recipes
COPY schemas /opt/moseq2/framework-source/schemas
COPY environments/legacy-worker-entrypoint.sh /usr/local/bin/moseq2-test-entrypoint

RUN uv venv /opt/moseq2/controller --python /usr/local/bin/python \
    && uv sync --frozen --no-dev --no-install-project \
      --project /opt/moseq2/framework-source \
    && uv build --wheel --project /opt/moseq2/framework-source \
      --out-dir /tmp/framework-wheel-preflight \
    && chmod 0755 /usr/local/bin/moseq2-test-entrypoint \
    && rm -rf /tmp/framework-wheel-preflight /tmp/worker-inputs

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/moseq2-test-entrypoint"]
CMD ["--version"]
