from setuptools import setup


setup(name='slotmachine',
      version='0.0.7',
      description='Conference talk scheduler',
      author='EMF',
      author_email='russ@emfcamp.org',
      url='https://github.com/emfcamp/slotmachine',
      packages=['slotmachine'],
      install_requires=['PuLP==3.0.2', # 2.8.0 # 2.1
                        'python-dateutil>=2.5.3'],
      license='MIT License',
      zip_safe=False,
      keywords='',
      classifiers=[])
