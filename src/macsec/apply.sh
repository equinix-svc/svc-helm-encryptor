# apply_key: program one QKD key into the kernel MACsec (static keys, no MKA). Sourced by entrypoint.sh.
# Needs MACSEC_IFACE (and MACSEC_ADDR, optional), and KEY KEY_ID AN CIPHER PEER_MAC from /macsec/key.env.
# Four SAs (association numbers 0 to 3) take the keys in turn: the new key is installed and made the sending one, then the old one goes.
IF="${MACSEC_LINK:-macsec0}"
apply_key() {
  ip link set "$MACSEC_IFACE" up || return 1
  if ! ip link show "$IF" >/dev/null 2>&1; then
    ip link add link "$MACSEC_IFACE" name "$IF" type macsec port 1 encrypt on cipher "$CIPHER" || return 1
  fi
  OLD=$(( (AN + 3) % 4 ))
  ip macsec del "$IF" tx sa "$AN" 2>/dev/null
  ip macsec add "$IF" tx sa "$AN" pn 1 on key "$KEY_ID" "$KEY" || return 1
  ip macsec add "$IF" rx port 1 address "$PEER_MAC" 2>/dev/null
  ip macsec del "$IF" rx port 1 address "$PEER_MAC" sa "$AN" 2>/dev/null
  ip macsec add "$IF" rx port 1 address "$PEER_MAC" sa "$AN" pn 1 on key "$KEY_ID" "$KEY" || return 1
  ip link set "$IF" type macsec encodingsa "$AN" || return 1
  ip macsec del "$IF" tx sa "$OLD" 2>/dev/null
  ip macsec del "$IF" rx port 1 address "$PEER_MAC" sa "$OLD" 2>/dev/null
  [ -z "${MACSEC_ADDR:-}" ] || ip addr replace "$MACSEC_ADDR" dev "$IF"
  ip link set "$IF" up
}
