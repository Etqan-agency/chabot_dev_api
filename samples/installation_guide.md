# Atlas-2 Pro — Installation Guide

## What's in the Box

Each Atlas-2 Pro shipment contains:

1. The Atlas-2 Pro robot (pre-assembled).
2. A docking station with a 2-meter detachable power cable.
3. A wireless charging mat for the on-board tablet.
4. Three RFID elevator beacons.
5. A printed quick-start card.
6. A USB-C cable for diagnostic access.

## Site Requirements

The Atlas-2 Pro requires:

- A flat operating surface with a maximum slope of 5°.
- Wi-Fi 6 coverage with at least -65 dBm signal strength in all
  intended operating zones.
- A standard EU-style 230 V / 50 Hz power outlet within 1.5 meters of
  the docking station.
- Door frames at least 84 cm wide.
- For multi-floor operation, an elevator with an open API or a
  Wi-Fi-controlled relay (we provide a recommended hardware list on
  request).

## First Boot

1. Place the docking station against a wall in your designated charging
   zone. Plug it in. The status LED turns solid green when ready.
2. Lift the Atlas-2 Pro onto the dock. It will boot automatically; the
   first boot takes approximately 4 minutes.
3. The on-board tablet displays a six-digit pairing code. Enter this
   code at https://manage.acmerobotics.example to associate the robot
   with your account.
4. Run the floor mapping wizard. Mapping a typical hotel floor takes
   12 to 25 minutes depending on layout complexity.

## Common Issues

- **Robot does not charge on the dock**: ensure the contact pads on the
  underside are clean. Wipe them with isopropyl alcohol if needed.
- **Wi-Fi disconnects on Floor 4**: most often a coverage problem.
  Walk the route with the diagnostic app in monitor mode; signal must
  stay above -70 dBm.
- **Elevator integration fails**: confirm the elevator beacon is
  flashing blue. A solid red light indicates a flat coin-cell battery
  (CR2032).

## Firmware Updates

Firmware updates are pushed automatically every Tuesday at 03:00 local
time. To install a critical security patch immediately, run
`atlas-update --force` from the maintenance console. Updates take
under 6 minutes and the robot is unavailable during the update.
