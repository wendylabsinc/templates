# {{.APP_ID}}

A minimal Embedded Swift app for Wendy Lite. It writes `Hello, world` to the
Wendy Lite console once and exits.

## Requirements

- A Wendy Lite device running current Wendy Lite firmware
- Wendy CLI access to the device
- Swift 6.3.2 and the matching Embedded Swift WebAssembly SDK on the development
  host
- Network access on the first build to fetch the `wendy-lite` Swift package

The current source does not make an HTTP or HTTPS request. `wendy.json` includes
a network entitlement so the project can be extended with networking.

## Run and verify

From the generated project directory:

```sh
wendy run
```

The streamed device output should contain `Hello, world` once.

## How it works

- `Sources/{{.APP_ID}}/main.swift` converts a `StaticString` to UTF-8 and passes
  it to `Console.print`.
- `Package.swift` enables Embedded Swift and links the `WendyLite` package as a
  WebAssembly guest.
- `wendy.json` selects the Wendy Lite platform and grants network access.

The package reserves 64 KiB of initial WebAssembly memory and an 8 KiB stack.
Embedded targets have limited memory, so avoid unbounded allocation.

## Extend it

- Change the string passed to `print`.
- Add a loop and `System.sleepMs` for periodic output.
- Use `WiFi`, `DNS`, `Net`, or `TLS` from the `WendyLite` package to turn this
  into a network example. Keep the existing network entitlement when doing so.
- Remove the network entitlement if the app will remain console-only.

## Operations and troubleshooting

Run the command again after changing the source. If no output appears, add a
second `Console.print` call and inspect the build and device output from
`wendy run` before adding more application logic.
