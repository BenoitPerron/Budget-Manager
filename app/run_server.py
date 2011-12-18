from __future__ import absolute_import, division
from application import create_app
from os import path

here = path.dirname(path.abspath(__file__))

activate_this = path.join(path.dirname(here), 'bin/activate_this.py')
execfile(activate_this, dict(__file__=activate_this))

if __name__ == '__main__':
    app = create_app('BUDGET_CONFIG_PATH')
    app.run()
