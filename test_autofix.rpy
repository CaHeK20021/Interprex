init python:
    class TestClass(Null):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.name = "Тестовый объект"

    def get_message(role_name):
        return "Вы получили роль " + role_name

label test_label:
    $ msg = "Роль " + role.name + " была удалена."
    $ broken_string = "Привет мир"
    return

screen test_screen():
    text "Привет"
    textbutton "Окей" action Return()
