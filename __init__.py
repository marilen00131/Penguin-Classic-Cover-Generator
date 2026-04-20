from calibre.customize import InterfaceActionBase


class CoverGeneratorPlugin(InterfaceActionBase):
    name = 'Cover Generator'
    description = 'Generate custom covers from metadata with preview'
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'OpenAI'
    version = (1, 6, 0)
    minimum_calibre_version = (6, 0, 0)

    actual_plugin = 'calibre_plugins.cover_generator.ui:CoverGeneratorUI'

    def is_customizable(self):
        return False
