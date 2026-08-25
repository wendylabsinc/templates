import WendyLite

let ledPin: Int32 = 8

@main
struct BlinkApp {
    static func main() {
        GPIO.configure(pin: ledPin, mode: .output)

        while true {
            GPIO.write(pin: ledPin, level: 1)
            System.sleepMs(500)
            GPIO.write(pin: ledPin, level: 0)
            System.sleepMs(500)
        }
    }
}
