#!/bin/bash
# Docker HEALTHCHECK without curl/python start-up cost: plain HTTP over bash /dev/tcp.
# Healthy = the instance answers GET /health/ready with 200.
exec 3<>"/dev/tcp/127.0.0.1/${FTUNI_PORT}" || exit 1
printf 'GET /health/ready HTTP/1.0\r\nHost: localhost\r\n\r\n' >&3
read -t 2 -r status <&3 || exit 1
[[ "$status" == *" 200 "* ]]
