# BranchMorphoGen Web GUI

This is a browser interface for BranchMorphoGen. You edit parameters in a panel, and the morphology grows on screen while the simulation runs.

The GUI does not change or link against the C++ core. It does three things:

1. It writes your parameters to a regular `parameters.in` file, in the same format the core reads.
2. It runs the unmodified `BranchMorphoGen` executable on that file, in a new folder under `webgui/runs/`.
3. It watches the files the simulator writes (`*-Conf-*.swc`, `*-TemproralProps-*.csv`, `*.tif`) and streams them to the browser as they appear.

It uses only the Python standard library, so there is nothing to install.

## Start

```bash
# 1. build the core once (skip if build/BranchMorphoGen already exists)
mkdir -p build && cd build && cmake .. && make -j && cd ..

# 2. start the GUI from the package folder
python3 webgui/server.py
```

Your browser opens at http://127.0.0.1:8050. Options:

| flag | meaning |
|---|---|
| `--port 9000` | use a different port |
| `--binary /path/to/BranchMorphoGen` | use a specific executable |
| `--build` | build the core with CMake if no executable is found |
| `--no-browser` | don't open a browser tab |

## Using it

- **Parameters (left).** Every key in `parameters.in` appears here, grouped as in the file. Changed values are highlighted. Hover over a name for its description.
  - **Time-dependent parameters** are edited as `time : value` tables: `LimitsX/Y/Z`, `StateVelocity`, `TransitionRates` and `BranchingRate`. Use "make constant" to switch a table to a single value.
  - **Matrices** are entered as rows separated by `;`, for example `0,0.69,0.51; 0.42,0,0.30; 0.67,0.71,0`.
  - **Checks.** Inputs are checked as you type: the number of states must match the number of velocities, the matrices must be NState × NState, and so on.
  - **Starting from an SWC.** For `ReadFromSWC`, upload an `.swc` file under `SWCFileName`.
- **Auto-run on change.** About a second after you stop editing, the current run is stopped and a new one starts.
- **Fast preview.** On by default. It overrides two values for the run only:
  - `pixelsize` becomes at least 2 µm, so writing the TIFF is cheap.
  - `N_SWC` becomes at least 40, so frames arrive often.

  Turn it off for a full-resolution run that uses exactly the values in the panel. The values actually used are always in `runs/<run>/parameters.in`.
- **Viewer.**
  - Scroll to zoom, drag to pan, double-click to fit the view.
  - In 3D, drag rotates the view and shift-drag pans.
  - Branches can be coloured by path distance, branch order, radius or depth.
  - The dashed box shows the time-dependent boundary at the frame's time.
  - "Synthetic image" shows the simulated microscopy TIFF for the frame.
- **Timeline.**
  - Scrub through the frames or press play. Arrow keys and the space bar also work.
  - "Follow live" jumps to each new frame as it arrives.
  - When `NSample > 1`, a sample selector appears.
- **Charts.** Tips, branches and total length are plotted against time, from `TemproralProps-*.csv`.
- **Files, Log and Runs (bottom right).**
  - Files: download the SWC, TIFF and BranchStats files for the current frame, plus the run's `parameters.in`.
  - Log: the simulator's own console output.
  - Runs: replay any earlier run, and load its parameters back into the panel.
- **Import / Export .in.** Load any parameter file, or save the current panel as one. The preset menu lists the `*.in` files in the package folder.

## Notes

- The core uses a fixed random seed (`GLOBAL_SEED` in `main.cpp`), so identical parameters give identical morphologies.
- Each frame is thinned to about 0.4 µm spacing before it is drawn, which keeps large trees fast. Downloaded files are the full-resolution originals.
- Run folders accumulate in `webgui/runs/`. Delete old ones whenever you like.
