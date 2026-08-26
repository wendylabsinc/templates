# {{.APP_ID}}

A minimal Embedded Swift app for Wendy Lite. It drives GPIO 8 high and low every
500 milliseconds to blink a board LED connected to that pin.

## Requirements

- A Wendy Lite device running current Wendy Lite firmware
- A board whose built-in LED is connected to GPIO 8, or an external LED with an
  appropriate resistor on GPIO 8
- Wendy CLI access to the device
- Swift 6.3.2 and the matching Embedded Swift WebAssembly SDK on the development
  host
- Network access on the first build to fetch the `wendy-lite` Swift package

## Run and verify

From the generated project directory:

```sh
wendy run
```

After the app starts, the LED should be on for about 500 milliseconds and off
for about 500 milliseconds in a loop.

## How it works

- `Sources/{{.APP_ID}}/main.swift` configures GPIO 8 as an output, writes high and
  low levels, and sleeps between writes.
- `Package.swift` enables Embedded Swift and links the `WendyLite` package as a
  WebAssembly guest.
- `wendy.json` selects the Wendy Lite platform and grants GPIO access.

The package reserves 64 KiB of initial WebAssembly memory and an 8 KiB stack.
Embedded targets have limited memory, so avoid unbounded allocation.

## Extend it

- Change `ledPin` in `main.swift` for a different board or external LED.
- Change both `System.sleepMs` values to adjust the blink pattern.
- Use `GPIO.setPWM` for brightness control, or add another Wendy Lite API from
  the `WendyLite` package and declare its entitlement in `wendy.json`.

## Operations and troubleshooting

Run the command again after changing the source. Add short `Console.print`
messages when debugging control flow.

If the app deploys but no LED changes, confirm the board's LED pin and polarity.
Some boards use an active-low LED, which reverses the visible high/low states.
