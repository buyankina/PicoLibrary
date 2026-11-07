# Feedback kiosk - Raspberry Pi Pico / MicroPython
# States and events follow the Lucidchart State Diagram.
from machine import Pin, PWM, I2C, time_pulse_us
import time
import neopixel


class Sensor:

    def tripped(self):
        return False


class UltrasonicSensor(Sensor):

    def __init__(self, trigger_pin, echo_pin, name):
        self.name = name
        self.trigger = Pin(trigger_pin, Pin.OUT, value=0)
        self.echo = Pin(echo_pin, Pin.IN)

    def distance_cm(self):
        self.trigger.value(0)
        time.sleep_us(2)
        self.trigger.value(1)
        time.sleep_us(10)
        self.trigger.value(0)
        pulse = time_pulse_us(self.echo, 1, 30000)
        return pulse / 58 if pulse > 0 else 999

    def tripped(self):
        return self.distance_cm() < 50


class Button:

    def __init__(self, pin, name):
        self.pin = Pin(pin, Pin.IN, Pin.PULL_UP)
        self.name = name
        self.raw = self.stable = self.pin.value()
        self.changed = time.ticks_ms()

    def pressed(self, now):
        value = self.pin.value()
        if value != self.raw:
            self.raw, self.changed = value, now
        if value != self.stable and time.ticks_diff(now, self.changed) >= 30:
            self.stable = value
            return value == 0
        return False


class Display:
    pass


class LCDDisplay(Display):
    # PCF8574: RS=P0, E=P2, backlight=P3, D4..D7=P4..P7.

    def __init__(self):
        self.bus = I2C(0, sda=Pin(0), scl=Pin(1), freq=100000)
        time.sleep_ms(50)
        for nibble in (0x30, 0x30, 0x30, 0x20):
            self.nibble(nibble)
            time.sleep_ms(5)
        for command in (0x28, 0x0C, 0x06, 0x01):
            self.send(command)
        time.sleep_ms(3)

    def nibble(self, value):
        self.bus.writeto(0x27, bytes([value | 8]))
        self.bus.writeto(0x27, bytes([value | 12]))
        time.sleep_us(1)
        self.bus.writeto(0x27, bytes([value | 8]))
        time.sleep_us(50)

    def send(self, value, data=0):
        self.nibble((value & 0xF0) | data)
        self.nibble(((value << 4) & 0xF0) | data)

    def message(self, text):
        lines = ['']
        for word in text.split():
            if lines[-1] and len(lines[-1]) + 1 + len(word) > 20:
                lines.append(word)
            else:
                lines[-1] += (' ' if lines[-1] else '') + word
        for row, address in enumerate((0x80, 0xC0, 0x94, 0xD4)):
            self.send(address)
            line = lines[row] if row < len(lines) else ''
            for char in (line + ' ' * 20)[:20]:
                self.send(ord(char), 1)


class Light:
    pass


class LightStrip(Light):

    def __init__(self):
        self.pixels = neopixel.NeoPixel(Pin(6), 8)

    def set(self, color):
        self.pixels.fill(color)
        self.pixels.write()


class Buzzer:
    pass


class PassiveBuzzer(Buzzer):

    def __init__(self):
        self.pwm = PWM(Pin(7))
        self.pwm.duty_u16(0)
        self.notes = []
        self.until = time.ticks_ms()

    def stop(self):
        self.notes = []
        self.pwm.duty_u16(0)

    def play(self, notes):
        self.notes = list(notes)
        self.until = time.ticks_ms()

    def update(self, now):
        if time.ticks_diff(now, self.until) >= 0:
            if self.notes:
                frequency, duration = self.notes.pop(0)
                if frequency:
                    self.pwm.freq(frequency)
                self.pwm.duty_u16(12000 if frequency else 0)
                self.until = time.ticks_add(now, duration)
            else:
                self.pwm.duty_u16(0)


class FeedbackStats:

    def __init__(self):
        self.reset()

    def reset(self):
        self.yes = self.no = self.consecutive_negative = 0

    def record(self, state):
        if state == 2:
            self.yes += 1
            self.consecutive_negative = 0
        else:
            self.no += 1
            self.consecutive_negative += 1
        print('STATS yes=%d no=%d negative_streak=%d' %
              (self.yes, self.no, self.consecutive_negative))

    def below_threshold(self):
        return self.consecutive_negative >= 3


class StateModel:

    def __init__(self):
        self.state = 0
        self.transitions = {}
        self.buttons = []
        self.sensors = []

    def addTransition(self, source, event, target):
        self.transitions[(source, event)] = target

    def addButton(self, button):
        self.buttons.append(button)

    def addSensor(self, sensor):
        self.sensors.append(sensor)

    def processEvent(self, event):
        target = self.transitions.get((self.state, event))
        if target is None:
            return False
        print('%d --%s--> %d' % (self.state, event, target))
        self.state = target
        return True


class FeedbackController:
    MESSAGES = ('Do you like me?', 'Do you like me? think twice and be nice',
                "you're very cute too", "Now we all know you stupid. next time be gentle",
                'calling the rude human police')

    def __init__(self):
        self.display = LCDDisplay()
        self.sensor = UltrasonicSensor(2, 3, 'uss')
        self.green = Button(4, 'green')
        self.red = Button(5, 'red')
        self.light = LightStrip()
        self.buzzer = PassiveBuzzer()
        self.stats = FeedbackStats()
        self.model = StateModel()
        self.model.addButton(self.green)
        self.model.addButton(self.red)
        self.model.addSensor(self.sensor)
        for transition in ((0, 'uss_trip', 1),(1, 'timer_timeout', 0),
                           (1, 'green_press', 2),(1, 'red_press', 3),
                           (2, 'timer_timeout', 0),(3, 'timer_timeout', 0),
                           (3, 'feedback_below_threshold', 4),(4, 'green_press', 0)):
            self.model.addTransition(*transition)
        self.near = False
        self.last_sensor = time.ticks_add(time.ticks_ms(), -100)
        self.entry()

    def entry(self):
        state = self.model.state
        self.display.message(self.MESSAGES[state])
        print('STATE:', ('Welcome', 'Selection', 'yes', 'no', 'Alarm')[state])
        print('LCD:', self.MESSAGES[state])
        self.buzzer.stop()
        if state == 0:
            self.light.set((0, 0, 80))
        elif state == 1:
            self.deadline = time.ticks_add(time.ticks_ms(), 5000)
            self.light.set((80, 60, 0))
        elif state in (2, 3):
            self.deadline = time.ticks_add(time.ticks_ms(), 5000)
            self.stats.record(state)
            self.light.set((0, 100, 0) if state == 2 else (100, 0, 0))
            if state == 2:
                self.buzzer.play([(523, 180),(659, 180),(784, 180),(1047, 350)])
            else:
                self.buzzer.play([(220, 250),(165, 250),(110, 350)])
        elif state == 4:
            self.light.set((100, 0, 60))
            self.buzzer.play([(880, 500),(0, 250),(880, 500),(0, 250),(880, 500)])

    def event(self, name):
        previous = self.model.state
        if self.model.processEvent(name):
            if previous == 4:
                self.stats.reset()
                print('STATS reset on exit Alarm')
            self.entry()

    def run(self):
        while True:
            now = time.ticks_ms()
            self.buzzer.update(now)
            green = self.green.pressed(now)
            red = self.red.pressed(now)
            if time.ticks_diff(now, self.last_sensor) >= 100:
                self.last_sensor = now
                self.near = self.sensor.tripped()
            state = self.model.state
            if state == 0:
                if self.near:
                    self.event('uss_trip')
            elif state == 1:
                if time.ticks_diff(now, self.deadline) >= 0:
                    self.event('timer_timeout')
                elif green and not red:
                    self.event('green_press')
                elif red and not green:
                    self.event('red_press')
            elif state in (2, 3):
                if time.ticks_diff(now, self.deadline) >= 0:
                    if state == 3 and self.stats.below_threshold():
                        self.event('feedback_below_threshold')
                    else:
                        self.event('timer_timeout')
            elif state == 4 and green:
                self.event('green_press')
            time.sleep_ms(5)


print('Feedback kiosk: sensor <50cm, 1=green, 2=red.')
print('Stay below 50cm for repeated rounds. Three red votes in a row trigger Alarm.')
FeedbackController().run()
