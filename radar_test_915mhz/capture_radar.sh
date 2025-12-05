#!/bin/bash

DURATION=30
SAMPLE_RATE=2560000
FREQ=915000000

echo "=== 915 MHz Bistatic Radar Capture ==="
echo "Duration: ${DURATION}s"
echo "Frequency: 915 MHz"
echo ""
echo "Starting in 3 seconds..."
sleep 3

# Start HackRF beacon transmission
echo "[$(date +%s.%N)] Starting HackRF beacon..."
hackrf_transfer -t ../beacon_900mhz_cw.iq -f $FREQ -s 10000000 -x 20 -a 1 -R > hackrf_tx.log 2>&1 &
HACKRF_PID=$!
sleep 2

# Start RTL-SDR captures
echo "[$(date +%s.%N)] Starting RTL-SDR left..."
rtl_sdr -d 0 -f $FREQ -s $SAMPLE_RATE -n $((SAMPLE_RATE * DURATION)) rtlsdr_left.bin > rtlsdr_left.log 2>&1 &
LEFT_PID=$!

echo "[$(date +%s.%N)] Starting RTL-SDR right..."
rtl_sdr -d 1 -f $FREQ -s $SAMPLE_RATE -n $((SAMPLE_RATE * DURATION)) rtlsdr_right.bin > rtlsdr_right.log 2>&1 &
RIGHT_PID=$!

# Start webcam capture
echo "[$(date +%s.%N)] Starting webcam..."
ffmpeg -f v4l2 -framerate 30 -video_size 1280x720 -i /dev/video0 -t $DURATION -q:v 2 frames_%06d.jpg > webcam.log 2>&1 &
WEBCAM_PID=$!

echo ""
echo "=== CAPTURING FOR ${DURATION} SECONDS ==="
echo "Move around in front of the antennas!"
echo ""

# Wait for RTL-SDR captures to complete
wait $LEFT_PID $RIGHT_PID

# Stop HackRF
echo "[$(date +%s.%N)] Stopping HackRF..."
kill $HACKRF_PID 2>/dev/null
wait $HACKRF_PID 2>/dev/null

# Wait for webcam
wait $WEBCAM_PID 2>/dev/null

echo "[$(date +%s.%N)] Capture complete!"
ls -lh *.bin *.jpg 2>/dev/null | head -10
