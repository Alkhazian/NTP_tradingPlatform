#!/bin/sh
# Generate htpasswd file from environment variables at container startup
# This ensures credentials are not stored in plain text in the repository

# htpasswd generation removed - using cookie-based auth

exec nginx -g 'daemon off;'
