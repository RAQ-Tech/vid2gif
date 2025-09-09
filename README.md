\# vid2gif



Dockerized WebUI for generating GIF previews from large video libraries.





\## Testing



Install the dependencies and run the test suite:



```bash

pip install -r requirements.txt

pytest

```





\## Installation



\### Local Python

1\. Clone the repository and install dependencies:

&nbsp;  ```bash

&nbsp;  git clone https://github.com/example/vid2gif.git

&nbsp;  cd vid2gif

&nbsp;  python -m venv .venv

&nbsp;  source .venv/bin/activate

&nbsp;  pip install -r requirements.txt

&nbsp;  ```

2\. Launch the application:

&nbsp;  ```bash

&nbsp;  python app/main.py

&nbsp;  ```



\### Docker

1\. Build the container:

&nbsp;  ```bash

&nbsp;  docker build -t vid2gif .

&nbsp;  ```

2\. Run the service, binding your video library and state directories:

&nbsp;  ```bash

&nbsp;  docker run \\

&nbsp;    -p 904:904 \\

&nbsp;    -e PUID=99 \\
&nbsp;    -e PGID=100 \\

&nbsp;    -v /path/to/videos:/library \\

&nbsp;    -v /path/to/state:/state \\

&nbsp;    vid2gif

&nbsp;  ```



\## Environment Variables

The application looks for a few environment variables to control where data lives:



| Variable    | Default      | Purpose                                  |

|-------------|--------------|------------------------------------------|

| `PUID`      | `99`         | User ID the app runs as                  |

| `PGID`      | `100`        | Group ID the app runs as                 |

| `LIB\_ROOT`  | `/library`   | Location of the video library            |

| `STATE\_ROOT`| `/state`     | Holds logs and temporary GIF output      |



These can be overridden when invoking `python app/main.py` or the Docker container, e.g. `docker run -e LIB\_ROOT=/media/videos ...`.



\## Example Workflow

1\. Start the server locally or via Docker as shown above.

2\. Visit \[http://localhost:904](http://localhost:904) and select a video or folder under the mounted library.

3\. Submit the job and monitor progress on the \*\*Live Logs\*\* page.

4\. Completed GIFs can be downloaded from the \*\*Completed\*\* tab.

\## Smooth Motion

Enable the **Smooth motion** option in the New Job form to generate
intermediate frames with ffmpeg's `minterpolate` filter when the
requested GIF FPS differs from the source video. This makes motion look
fluid but can significantly increase processing time.

\## Contributing

\* Follow \[PEP8](https://peps.python.org/pep-0008/) style guidelines; automated formatting with `black` is encouraged.

\* Add tests under \[`tests/`](tests/) and ensure they pass with `pytest` before submitting a pull request.

\* The core application logic lives in \[`app/main.py`](app/main.py); an example test suite is in \[`tests/test\_main.py`](tests/test\_main.py).



Thanks for contributing!

