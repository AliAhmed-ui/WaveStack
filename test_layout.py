import tkinter as tk
import wavestack
import time
import threading

def run_test():
    app = wavestack.WaveStackApp()
    
    # Give it a moment to build the layout
    app.update()
    
    print("Main window size:", app.winfo_width(), "x", app.winfo_height())
    print("Status bar mapped:", app.status_bar.winfo_ismapped())
    print("Status bar Y:", app.status_bar.winfo_y())
    print("Status bar height:", app.status_bar.winfo_height())
    
    app.destroy()

if __name__ == '__main__':
    # run in a separate thread to allow mainloop if needed, or just update
    # Note: we don't have a display server in the sandbox, so this might fail.
    # Let's try to run xvfb-run
    pass
