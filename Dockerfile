FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY packages ./packages

RUN pip install --no-cache-dir -e ".[mock]" \
    && mkdir -p /data

ENV SEATPOOL_DB=/data/gsuite_seatpool.db
EXPOSE 8788

ENTRYPOINT ["gsuite-seatpool"]
CMD ["mock", "--host", "0.0.0.0", "--port", "8788"]
