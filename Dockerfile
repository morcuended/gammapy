# We use a build stage to build a wheel which we then copy and install
# in the second stage to minimize image size. This is mostly needed
# because setuptools_scm needs the full version info from git and git
# itself but including that in the final image would bloat its size.

# We use a python image which comes with tools needed to build and compile
# python packages
FROM python:3.12 AS builder

# add necessary sources, including .git for version info
COPY . /repo/

# Build the wheel
RUN python -m pip install --no-cache-dir build \
    && cd repo \
    && python -m build --wheel


# Second stage, copy and install wheel using the official python image
# in the slim variant to reduce image size.
FROM python:3.12-slim
COPY --from=builder /repo/dist /tmp/dist

RUN python -m pip install --no-cache-dir /tmp/dist/* \
    && rm -r /tmp/dist

RUN useradd --create-home --system --user-group gammapy
USER gammapy
