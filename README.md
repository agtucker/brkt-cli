**brkt-cli** is a command-line interface to the
[Bracket Computing](http://www.brkt.com) service. It produces an
encrypted version of an operating system image that runs on
[Amazon Web Services](aws.md) (AWS), [Google Compute Platform](gce.md)
(GCP), or [VMware ESX](esx.md). The resulting encrypted image can then
be launched in the same manner as the original.  Please see the
links above for cloud-provider specific details and an overview of
the encryption process.

**brkt-cli** also has commands for managing private/public key pairs
and generating a JSON Web Token (JWT) that is used to authenticate with
The Bracket Computing service.

The latest release of **brkt-cli** is [1.0.10](https://github.com/brkt/brkt-cli/releases/tag/v1.0.10).

## Requirements

In order to use the Bracket service, you must be a
registered Bracket Computing customer.  Email support@brkt.com for
more information.

**brkt-cli** requires Python 2.7.

We recommend using [virtualenv](https://virtualenv.pypa.io/), to avoid
conflicts between **brkt-cli** dependencies and Python packages that are managed
by the operating system.  If you're not familiar with virtualenv, check out the
[Virtual Environments](http://docs.python-guide.org/en/latest/dev/virtualenvs/)
section of _The Hitchhiker's Guide to Python_.

## Installation

Use pip to install **brkt-cli** and its dependencies:

```
$ pip install brkt-cli
```

To install the most recent **brkt-cli** code from the tip of the master branch, run

```
$ pip install --upgrade git+https://github.com/brkt/brkt-cli.git
```

The master branch has the latest features and bug fixes, but is not as thoroughly
tested as the official release.

You can also run **brkt-cli** in a [Docker container](#docker).

If you need to [manage your own keys and tokens](cryptography.md),
you'll need to install the Python [cryptography](https://cryptography.io/)
library.

## Authentication

Many **brkt-cli** commands require an API token.  The
`brkt auth` command gets an API token from the Bracket service based
on your email and password, and prints the token to stdout:

```
$ brkt auth
Email: me@example.com
Password:
eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2V...
```

After authenticating, set the `$BRKT_API_TOKEN` environment variable:

```
$ export BRKT_API_TOKEN=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2V...
```

## Encrypting an image

See the [AWS](aws.md), [GCP](gce.md) or [VMware](esx.md) pages for
platform-specific documentation on encrypting and updating an image.

## <a name="docker"/>Running in a Docker container

**brkt-cli** ships with a `Dockerfile`, which allows you to run the `brkt`
command in a Docker container. This creates a completely isolated environment,
and avoids issues with Python libraries and platform-specific
binaries.  To download the **brkt-cli** source and build the `brkt` container:

```
$ export BRKT_API_RELEASE=1.0.10
$ wget https://github.com/brkt/brkt-cli/archive/v$BRKT_API_RELEASE.zip
$ unzip v$BRKT_API_RELEASE.zip
$ cd brkt-cli-$BRKT_API_RELEASE
$ docker build -t brkt .
```

Be sure to substitute the actual release number for `<RELEASE-NUMBER>`.  Once
the container is built, you can run it with the `docker run`
command.  Note that you must pass any required environment variables or
files into the container.  Some examples:

```
$ docker run --rm -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID \
-e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
brkt aws encrypt --region us-west-2 ami-9025e1f0
```
