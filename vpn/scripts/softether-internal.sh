#!/usr/bin/env bash

source <(curl -s "https://gist.githubusercontent.com/cenk1cenk2/e03d8610534a9c78f755c1c1ed93a293/raw/logger.sh")

log_this "" "$0" "LIFETIME"
log_divider

case $1 in
	up)
		log_start "Getting IP address from DHCP server..."
		sudo dhclient -v vpn_vpn
		log_start "Adding routes..."
		sudo sudo ip route add 192.168.16.0/20 via 192.168.128.1
		sudo sudo ip route add 192.168.192.0/22 via 192.168.128.1
		log_start "Updating DNS servers."
		echo "nameserver 192.168.128.1" | sudo tee /etc/resolv.conf
		log_start "DNS configuration file:"
		cat /etc/resolv.conf
		;;

	down)
		log_start "Killing DHCP server."
		sudo dhclient -v -r -d vpn_vpn
		log_start "Refreshing DHCP server on default gateway."
		INTERFACE="$(ip route | awk '/default/ { print $5 }')"
		log_start "DHCLIENT: $INTERFACE"
		sudo dhclient "$INTERFACE"
		log_start "DNS configuration file:"
		cat /etc/resolv.conf
		;;

	*)
		log_error "Please provide a valid argument. $0 [up/down]"
		;;
esac
