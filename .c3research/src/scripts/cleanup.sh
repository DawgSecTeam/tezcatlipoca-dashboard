#!/bin/bash
echo "This cleanup process KILLS anything using 5173/3000"
pkill -f uvicorn
pkill -f react
fuser -k 3000/tcp
fuser -k 5173/tcp
reset
