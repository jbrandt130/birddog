#!/bin/bash -f

PORT=8000
STORE=cache/dynalite

mkdir -p $STORE
echo Starting dynalite database on port $PORT, using store $STORE...
dynalite --port=$PORT --path=$STORE 
