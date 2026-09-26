/*
  secrets.example.h — template for Flipee's local credentials.

  Copy this file to `secrets.h` in the same folder and fill in your own
  values. `secrets.h` is gitignored, so your WiFi passphrase and relay key
  stay on your machine and never reach the repo:

      copy Flipee\secrets.example.h Flipee\secrets.h

  Flipee.ino includes secrets.h and will refuse to compile without it, so a
  fresh clone fails loudly with a "copy secrets.example.h" message rather
  than silently building a sketch that can't connect to anything.
*/

#pragma once

// --- WiFi ---
// Optional. Leave both blank ("") for pure foraging — Flipee will only ever
// join open networks. Set them to prefer one known network when it's
// visible, which is mainly useful for testing at your desk.
#define HOME_SSID_VAL      ""
#define HOME_PASSWORD_VAL  ""

// --- Optional Claude relay (see server/flipee_relay.py) ---
// SERVER_HOST_VAL is your relay's domain or IP. RELAY_KEY_VAL must match
// FLIPEE_RELAY_KEY in the relay's .env — if it doesn't, /reflect answers
// 401 and Flipee quietly falls back to a templated reflection.
//
// A bare LAN IP here is a DHCP lease and can move; prefer a domain or a
// static reservation for anything you don't want to re-flash.
#define SERVER_HOST_VAL    "relay.example.com"
#define RELAY_KEY_VAL      "change-me-to-a-long-random-string"
