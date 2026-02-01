#!/bin/sh
# Generate htpasswd file from environment variables at container startup
# This ensures credentials are not stored in plain text in the repository

# Install htpasswd (from apache2-utils)
apk add --no-cache apache2-utils > /dev/null 2>&1

if [ -n "$DASHBOARD_USER" ] && [ -n "$DASHBOARD_PASSWORD" ]; then
    htpasswd -bc /etc/nginx/htpasswd "$DASHBOARD_USER" "$DASHBOARD_PASSWORD"
    echo "Generated htpasswd for user: $DASHBOARD_USER"
else
    echo "WARNING: DASHBOARD_USER or DASHBOARD_PASSWORD not set - auth disabled"
    # Create empty htpasswd to prevent nginx errors
    touch /etc/nginx/htpasswd
fi

exec nginx -g 'daemon off;'
