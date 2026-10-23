#!/bin/bash
set -euxo pipefail

dnf update -y
dnf install -y python3.11 python3.11-pip git awscli

mkdir -p /opt/source-latency-profiler /var/log/source-latency-profiler
cat >/opt/source-latency-profiler/runner.env <<EOF
RESULTS_BUCKET=${results_bucket}
AWS_REGION=${aws_region}
EOF

cat >/etc/motd <<EOF
Source Latency Profiler runner
Results bucket: s3://${results_bucket}
Region: ${aws_region}

Install or sync the profiler package, then run latency-profiler probe with --upload-s3.
EOF
