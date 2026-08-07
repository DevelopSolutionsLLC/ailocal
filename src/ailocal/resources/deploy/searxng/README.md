# SearXNG Web Search Backend

This directory contains a self-hosted web search backend for your local AI stack.

## Overview

This SearXNG instance provides:
- Free, self-hosted web search capabilities
- JSON API responses for integration with LiteLLM
- No external API keys required
- Runs entirely within Docker

## Starting the Service

```bash
docker compose up -d
```

## Testing

Test that the service is working:

```bash
curl "http://localhost:8080/search?q=litellm&format=json"
```

Expected response should be a JSON object with search results.

## Troubleshooting

If the service doesn't start:
1. Check if port 8080 is already in use
2. Verify Docker is running
3. Check container logs: `docker logs searxng`

If search results are empty:
1. Verify network connectivity between containers
2. Check that the search engines are properly configured
3. Review logs for any errors

## Integration with LiteLLM

This service integrates with your existing LiteLLM setup through web search interception.
