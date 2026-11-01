# Local installation. Debian packages continue to use debian/rules.
PREFIX ?= /usr/local
BINDIR ?= $(PREFIX)/bin
LIBDIR ?= $(PREFIX)/lib/web-monitor
SYSCONFDIR ?= /etc/radio
SYSTEMDUNITDIR ?= /etc/systemd/system
VENV ?= $(LIBDIR)/.venv
PYTHON ?= python3
# Override with /usr/bin/python3 to use distro-managed dependencies instead.
APP_PYTHON ?= $(VENV)/bin/python
DESTDIR ?=
INSTALL ?= install

MODULES = server.py core.py demo.py discovery.py
ASSETS = static/index.html static/app.js static/style.css

.PHONY: all help install install-app install-config install-service install-deps test
all: help

help:
	@printf '%s\n' \
	  'Local install (defaults to /usr/local):' \
	  '  sudo make install-deps   Create private venv and install dependencies' \
	  '  sudo make install        Install code, assets, launcher, config and unit' \
	  '  sudo make install-app    Update code/assets/launcher only' \
	  '  make test PYTHON=...     Run tests with a dependency-equipped Python' \
	  '' \
	  'Existing /etc/radio/web-monitor.json is preserved.' \
	  'No target starts, enables, reloads or restarts a systemd service.' \
	  'DESTDIR supports staging; install-deps is deliberately not stageable.' \
	  'For distro dependencies: sudo make install APP_PYTHON=/usr/bin/python3'

install: install-app install-config install-service

install-app:
	$(INSTALL) -d -m 0755 "$(DESTDIR)$(LIBDIR)" "$(DESTDIR)$(LIBDIR)/static" "$(DESTDIR)$(BINDIR)"
	$(INSTALL) -m 0644 $(MODULES) "$(DESTDIR)$(LIBDIR)/"
	$(INSTALL) -m 0644 $(ASSETS) "$(DESTDIR)$(LIBDIR)/static/"
	@printf '%s\n' '#!/bin/sh' 'exec "$(APP_PYTHON)" -B -u -s "$(LIBDIR)/server.py" "$$@"' > "$(DESTDIR)$(BINDIR)/web-monitor"
	chmod 0755 "$(DESTDIR)$(BINDIR)/web-monitor"

install-config:
	$(INSTALL) -d -m 0755 "$(DESTDIR)$(SYSCONFDIR)"
	@if test -e "$(DESTDIR)$(SYSCONFDIR)/web-monitor.json" || test -L "$(DESTDIR)$(SYSCONFDIR)/web-monitor.json"; then \
	  printf '%s\n' 'Keeping existing configuration'; \
	else \
	  $(INSTALL) -m 0644 config.example.json "$(DESTDIR)$(SYSCONFDIR)/web-monitor.json"; \
	fi

install-service:
	$(INSTALL) -d -m 0755 "$(DESTDIR)$(SYSTEMDUNITDIR)"
	sed -e 's|^WorkingDirectory=.*|WorkingDirectory=$(LIBDIR)|' \
	    -e 's|^LoadCredential=.*|LoadCredential=web-monitor.json:$(SYSCONFDIR)/web-monitor.json|' \
	    -e 's|^ExecStart=.*|ExecStart=$(BINDIR)/web-monitor --config %d/web-monitor.json|' \
	    debian/web-monitor.service > "$(DESTDIR)$(SYSTEMDUNITDIR)/web-monitor.service"
	chmod 0644 "$(DESTDIR)$(SYSTEMDUNITDIR)/web-monitor.service"

# Kept separate from install: installation/staging never implicitly uses pip.
install-deps:
	@test -z "$(DESTDIR)" || { printf '%s\n' 'install-deps does not support DESTDIR' >&2; exit 1; }
	$(INSTALL) -d -m 0755 "$(LIBDIR)"
	$(PYTHON) -m venv "$(VENV)"
	"$(VENV)/bin/python" -m pip install -r requirements.txt

test:
	$(PYTHON) -m unittest -v
