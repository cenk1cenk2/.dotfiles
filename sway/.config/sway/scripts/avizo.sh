#!/usr/bin/env bash

PERCENTAGE=$1
TYPE=$2
case "$TYPE" in
	v)
		if [ "$PERCENTAGE" -eq 0 ]; then
			IMAGE='volume_muted'
		elif [ "$PERCENTAGE" -le 33 ]; then
			IMAGE='volume_low'
		elif [ "$PERCENTAGE" -le 66 ]; then
			IMAGE='volume_medium'
		else
			IMAGE='volume_high'
		fi
		;;
	b)
		if [ "$PERCENTAGE" -le 33 ]; then
			IMAGE='brightness_low'
		elif [ "$PERCENTAGE" -le 66 ]; then
			IMAGE='brightness_medium'
		else
			IMAGE='brightness_high'
		fi
		;;
	m)
		if [ "$PERCENTAGE" -eq 0 ]; then
			IMAGE='mic_muted'
		else
			IMAGE='mic_unmuted'
		fi
		;;
esac

PROGRESS=$(awk -v P="$PERCENTAGE" 'BEGIN { printf "%.2f", P / 100; exit 0 }')

avizo-client --progress="$PROGRESS" --image-resource="$IMAGE"
