#!/bin/bash

SERVICE_NAME=iptv-cartoons.service

echo "🔄 Restarting $SERVICE_NAME..."

systemctl daemon-reload
systemctl restart $SERVICE_NAME

STATUS=$(systemctl is-active $SERVICE_NAME)

if [ "$STATUS" = "active" ]; then
    echo "✅ $SERVICE_NAME restarted successfully!"
    systemctl status iptv-cartoons.service
else
    echo "❌ Failed to restart $SERVICE_NAME. Check logs:"
    journalctl -u $SERVICE_NAME --no-pager | tail -n 20
fi