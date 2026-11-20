# AWS Emulator image, as documented in the AWS Emulator User Guide
# ("Getting started with Docker"). There is no prebuilt image: this installs the
# Linux RPM. EMULATOR_ARCH is aarch64 on Apple silicon / Graviton, x86_64 otherwise.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal
ARG EMULATOR_ARCH=x86_64
RUN curl -fsSL -o /tmp/aws-emulator.rpm \
        "https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/latest/aws-emulator-${EMULATOR_ARCH}.rpm" \
    && rpm -i /tmp/aws-emulator.rpm && rm /tmp/aws-emulator.rpm \
    && mkdir -p /home/emulator && chown 1000 /home/emulator
USER 1000
ENV HOME=/home/emulator \
    EMULATOR_PORT=9297
EXPOSE 9297
# Loopback health probe inside the container. -k because the emulator's CA is
# regenerated on every start; the probe never leaves 127.0.0.1.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsk "https://127.0.0.1:${EMULATOR_PORT}/__admin/health" || exit 1
# 0.0.0.0 inside the container only; compose publishes the port on 127.0.0.1.
ENTRYPOINT ["aws-emulator", "-b", "0.0.0.0"]
