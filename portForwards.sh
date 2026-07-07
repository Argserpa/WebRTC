#!/bin/bash

kubectl port-forward svc/prometheus 9090:9090 -n streaming &
kubectl port-forward --address 0.0.0.0 svc/video-streamer 8081:8081 -n streaming &
kubectl port-forward --address 0.0.0.0 svc/hls-web 8080:80 -n streaming &

wait
