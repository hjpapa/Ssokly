from ui.tk_lifecycle import prepare_destroy


def destroy_root(root):
    prepare_destroy(root)
    root.destroy()
