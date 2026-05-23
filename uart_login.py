#!/usr/bin/env python
import argparse
import re
import sys
import time

import serial


def wait_and_login(port, baud, username, password, timeout):
    ser = serial.Serial(
        port,
        baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.2,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )

    print(f"Opened {port} at {baud} 8N1, no flow control")
    ser.write(b"\r\n")
    ser.flush()

    buffer = ""
    sent_user = False
    sent_password = False
    shell_prompt = False
    end_time = time.time() + timeout

    try:
        while time.time() < end_time:
            data = ser.read(4096)
            if not data:
                if shell_prompt:
                    break
                time.sleep(0.05)
                continue

            text = data.decode("utf-8", errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()

            buffer = (buffer + text)[-4000:]
            lower_buffer = buffer.lower()

            if re.search(r"root@[^\r\n]*[#>]\s*$", buffer) and not shell_prompt:
                print("\n[already at root shell prompt]")
                shell_prompt = True
                end_time = min(end_time, time.time() + 5)
                continue

            if not sent_user and re.search(
                r"(?:^|[\r\n])[^\r\n]*(login|username)\s*:\s*$",
                lower_buffer,
                re.MULTILINE,
            ):
                print("\n[detected login prompt, sending username]")
                ser.write((username + "\r\n").encode())
                ser.flush()
                sent_user = True
                buffer = ""
                continue

            if sent_user and not sent_password and re.search(
                r"password\s*:\s*$",
                lower_buffer,
                re.MULTILINE,
            ):
                print("\n[detected password prompt, sending password]")
                ser.write((password + "\r\n").encode())
                ser.flush()
                sent_password = True
                buffer = ""
                continue

            if sent_password and not shell_prompt and re.search(r"[#>$]\s*$", buffer):
                print("\n[login appears complete: shell prompt detected]")
                shell_prompt = True
                end_time = min(end_time, time.time() + 10)
    finally:
        ser.close()
        print("\nClosed serial port")

    print(
        f"sent_user={sent_user} "
        f"sent_password={sent_password} "
        f"shell_prompt={shell_prompt}"
    )
    return 0 if shell_prompt else 1


def parse_args():
    parser = argparse.ArgumentParser(
        description="Watch a UART login console and enter credentials automatically."
    )
    parser.add_argument("--port", default="COM20", help="Serial port, default: COM20")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate, default: 115200")
    parser.add_argument("--username", default="root", help="Login username, default: root")
    parser.add_argument("--password", default="toor", help="Login password, default: toor")
    parser.add_argument("--timeout", type=int, default=120, help="Seconds to wait, default: 120")
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        return wait_and_login(
            args.port,
            args.baud,
            args.username,
            args.password,
            args.timeout,
        )
    except serial.SerialException as exc:
        print(f"Serial error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
