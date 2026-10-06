cytoscape.min.js v3.30.2 - https://js.cytoscape.org/
MIT licence, full text in cytoscape-LICENSE.txt
Vendored so the generated topology HTML opens offline, with no CDN.

icons/*.svg - device drawings for the map (switch, router, server, BMC,
access point, phone, unknown). Original line art, not a vendor stencil set,
so no third-party artwork is redistributed. Regenerate with:
    python tools/make_icons.py
Each file keeps an __ACCENT__ placeholder that the renderer replaces with the
colour of the device kind before embedding the icon as a base64 data URI.
