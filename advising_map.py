import tkinter as tk
from tkinter import filedialog, messagebox
import pandas as pd
import matplotlib.pyplot as plt
import os

def main():
    root = tk.Tk()
    root.withdraw()

    # 1) Pick CSV
    csv_path = filedialog.askopenfilename(
        title="Select filled_pathway CSV",
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
    )
    if not csv_path:
        messagebox.showinfo("CSV to PNG", "No CSV selected.")
        return

    # Try to read CSV just to confirm it exists and is readable
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        messagebox.showerror("CSV to PNG", f"Failed to read CSV:\n{e}")
        return

    # 2) Create a blank 16:9 figure
    fig = plt.figure(figsize=(16, 9), dpi=150)
    ax = fig.add_subplot(111)
    ax.axis("off")

    # =======================================================================
    # [ INSERT DRAWING / FORMATTING CODE HERE ]
    # --- Horizontal divider line 1/4 down the image ---
    line_y = 0.2  # 1/4 down from top (Axes coordinates: 1 = top, 0 = bottom)
    ax.plot(
        [0, 1], [line_y, line_y],
        transform=ax.transAxes,
        color="black",
        linewidth=2
    )

    # --- 8 vertical sub-buckets in the upper 0.8 of the canvas ---
    # Uses your divider at y=0.2 (top region height = 0.8)
    import matplotlib.patches as patches

    divider_y = locals().get("line_y", 0.2)  # use existing line_y if defined; else 0.2

    # Gap = 2 pixels converted to Axes units
    width_px = fig.get_figwidth() * fig.dpi
    gap_ax = 2.0 / width_px
    n_cols = 8
    total_gap = gap_ax * (n_cols - 1)
    col_w = (1.0 - total_gap) / n_cols
    col_h = 1.0 - divider_y

    x = 0.0
    for _ in range(n_cols):
        rect = patches.Rectangle(
            (x, divider_y), col_w, col_h,
            linewidth=1.0,
            edgecolor="black",
            facecolor="none",
            transform=ax.transAxes,
            zorder=3
        )
        ax.add_patch(rect)
        x += col_w + gap_ax
    # Example:
    # ax.text(0.5, 0.5, "Advising Map Placeholder", ha="center", va="center", fontsize=16)
    #
    # You can use 'df' (the loaded DataFrame) to pull data from the CSV
    # and draw text boxes, lines, or formatted content.
    # =======================================================================
    # --- Student name from 'full_name' robustly (case-insensitive, trims blanks) ---
    def _find_col(df, target):
        tgt = target.replace("_", "").replace(" ", "").lower()
        for c in df.columns:
            key = str(c).replace("_", "").replace(" ", "").lower()
            if key == tgt:
                return c
        return None

    full_col = _find_col(df, "full_name")
    if full_col is not None:
        s = df[full_col].astype(str).str.strip()
        s = s[~s.str.lower().isin(["", "nan", "none"])]
        student_name = s.iloc[0] if not s.empty else "Student"
    else:
        student_name = "Student"

    # Find the earliest 4-digit year from the term-like column
    term_col = None
    for cand in ["match_term_code", "term_code", "match_term", "term_label", "term"]:
        if cand in df.columns:
            term_col = cand
            break

    start_year = "—"
    if term_col:
        years = (
            df[term_col]
            .dropna()
            .astype(str)
            .str.extract(r"(\d{4})")[0]
            .dropna()
        )
        if not years.empty:
            start_year = int(years.astype(int).min())


    # Draw padded header area
    try:
        from matplotlib import patches
    except ImportError:
        import matplotlib.patches as patches

    top_pad_height = 0.08  # 8% of image height
    header = patches.Rectangle(
        (0, 1 - top_pad_height), 1, top_pad_height,
        transform=ax.transAxes,
        facecolor="white", edgecolor="black", linewidth=1.5, zorder=5
    )
    ax.add_patch(header)

    ax.text(
        0.02, 1 - top_pad_height / 2,
        f"Student Name: {student_name}",
        transform=ax.transAxes, va="center", ha="left", fontsize=12, zorder=6
    )
    ax.text(
        0.70, 1 - top_pad_height / 2,
        f"Start Year: {start_year if start_year is not None else '—'}",
        transform=ax.transAxes, va="center", ha="left", fontsize=12, zorder=6
    )



    # Draw a 5-pixel black border around the entire figure
    border_thickness = 5 / fig.dpi  # convert pixels to inches
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    rect = plt.Rectangle(
        (0, 0), 1, 1,
        linewidth=border_thickness * fig.dpi,  # keep visually 5px thick
        edgecolor='black',
        facecolor='none',
        transform=ax.transAxes,
        zorder=10
    )
    ax.add_patch(rect)

    # Placeholder text so it runs even before edits
    ax.text(0.5, 0.5, "Blank image placeholder", ha="center", va="center", fontsize=16)

    # 3) Pick save location
    default_name = os.path.splitext(os.path.basename(csv_path))[0] + "_blank.png"
    save_path = filedialog.asksaveasfilename(
        title="Save PNG",
        defaultextension=".png",
        filetypes=[("PNG Image", "*.png")],
        initialfile=default_name
    )
    if not save_path:
        messagebox.showinfo("CSV to PNG", "No save location selected.")
        return

    # 4) Save
    try:
        fig.savefig(save_path, bbox_inches="tight")
        messagebox.showinfo("CSV to PNG", f"Saved PNG:\n{save_path}")
    except Exception as e:
        messagebox.showerror("CSV to PNG", f"Failed to save PNG:\n{e}")
    finally:
        plt.close(fig)

if __name__ == "__main__":
    main()