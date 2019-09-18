# -*- coding: utf-8 -*-
# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:hydrogen
#     text_representation:
#       extension: .py
#       format_name: hydrogen
#       format_version: '1.2'
#       jupytext_version: 1.2.0
#   kernelspec:
#     display_name: deepbedmap
#     language: python
#     name: deepbedmap
# ---

# %% [markdown]
# # **DeepBedMap**
#
# Predicting the bed elevation of Antarctica using our trained Super Resolution Deep Neural Network.
# The results will be compared against other interpolated grid products along groundtruth tracks in small regions.
# Finally we will produce an Antarctic-wide DeepBedMap Digital Elevation Model (DEM) at the very end!

# %%
import dataclasses
import math
import os
import typing

os.environ["CUDA_VISIBLE_DEVICES"] = ""

import xarray as xr
import salem

import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import axes3d
import numpy as np
import pandas as pd
import pygmt as gmt
import quilt
import rasterio
import skimage

import comet_ml
import chainer
import cupy

from features.environment import _load_ipynb_modules, _download_model_weights_from_comet

data_prep = _load_ipynb_modules("data_prep.ipynb")

# %% [markdown]
# # 1. Gather datasets

# %% [markdown]
# ## 1.1 Get bounding box of our area of interest
#
# Basically predict on an place where we have groundtruth data to validate against.

# %%
def get_image_with_bounds(filepaths: list, indexers: dict = None) -> xr.DataArray:
    """
    Retrieve raster image in xarray.DataArray format patched
    with projected coordinate bounds as (xmin, ymin, xmax, ymax)

    Note that if more than one filepath is passed in,
    the output groundtruth image array will not be valid
    (see https://github.com/pydata/xarray/issues/2159),
    but the window_bound extents will be correct
    """

    with xr.open_mfdataset(
        paths=filepaths, combine="nested", concat_dim=None
    ) as dataset:
        # Retrieve dataarray from NetCDF datasets
        dataarray = dataset.z.isel(indexers=indexers)

    # Patch projection information into xarray grid
    dataarray.attrs["pyproj_srs"] = "epsg:3031"
    sgrid = dataarray.salem.grid.corner_grid
    assert sgrid.origin == "lower-left"  # should be "lower-left", not "upper-left"

    # Patch bounding box extent into xarray grid
    if len(filepaths) == 1:
        left, right, bottom, top = sgrid.extent
    elif len(filepaths) > 1:
        print("WARN: using multiple inputs, output groundtruth image may look funny")
        x_offset, y_offset = sgrid.dx / 2, sgrid.dy / 2
        left, right = (
            float(dataarray.x[0] - x_offset),
            float(dataarray.x[-1] + x_offset),
        )
        assert sgrid.x0 == left
        bottom, top = (
            float(dataarray.y[0] - y_offset),
            float(dataarray.y[-1] + y_offset),
        )
        assert sgrid.y0 == bottom  # dataarray.y.min()-y_offset

    # check that y-axis and x-axis lengths are divisible by 4
    try:
        shape = int((top - bottom) / sgrid.dy), int((right - left) / sgrid.dx)
        assert all(i % 4 == 0 for i in shape)
    except AssertionError:
        print(f"WARN: Image shape {shape} should be divisible by 4 for DeepBedMap")
    finally:
        dataarray.attrs["bounds"] = [left, bottom, right, top]

    return dataarray


# %%
test_filepaths = ["highres/2007tx", "highres/2010tr", "highres/istarxx"]
groundtruth = get_image_with_bounds(
    filepaths=[f"{t}.nc" for t in test_filepaths],
    # indexers={"y": slice(1, -2), "x": slice(1, -2)},  # for 2007tx
    indexers={"x": slice(1, -2)},  # for 2007tx, 2010tr and istarxx
)
window_bound = rasterio.coords.BoundingBox(*groundtruth.bounds)
print(window_bound)

# %% [markdown]
# ## 1.2 Get neural network input datasets
#
# Collect BEDMAP2 (X), REMA (W1), MEaSUREs Ice Flow (W2) and Antarctic Snow Accumulation (W3) datasets
# cropped to our area of interest that will be fed into our trained neural network later.

# %%
def get_deepbedmap_model_inputs(
    window_bound: rasterio.coords.BoundingBox,
    padding: int = 1000,
    use_whole_rema: bool = False,
) -> (np.ndarray, np.ndarray, np.ndarray, np.ndarray):
    """
    Outputs one large tile for each of:
    BEDMAP2, REMA, MEASURES Ice Flow Velocity and Antarctic Snow Accumulation
    according to a given window_bound in the form of (xmin, ymin, xmax, ymax).
    """
    data_prep = _load_ipynb_modules("data_prep.ipynb")

    if window_bound == rasterio.coords.BoundingBox(
        left=-1_594_000.0, bottom=-166_500.0, right=-1_575_000.0, top=-95_500.0
    ):
        # Quickly pull from cached quilt storage if using (hardcoded) test region
        quilt.install(package="weiji14/deepbedmap/model/test", force=True)
        pkg = quilt.load(pkginfo="weiji14/deepbedmap/model/test")
        X_tile = pkg.X_tile()
        W1_tile = pkg.W1_tile()
        W2_tile = pkg.W2_tile()
        W3_tile = pkg.W3_tile()
    else:
        X_tile = data_prep.selective_tile(
            filepath="lowres/bedmap2_bed.tif",
            window_bounds=[[*window_bound]],
            padding=padding,
            gapfiller=-5000.0,
        )
        W3_tile = data_prep.selective_tile(
            filepath="misc/Arthern_accumulation_bedmap2_grid1.tif",
            window_bounds=[[*window_bound]],
            padding=padding,
            gapfiller=0.0,
        )
        W2_tile = np.concatenate(
            [
                data_prep.selective_tile(
                    filepath="netcdf:misc/antarctic_ice_vel_phase_map_v01.nc:VX",
                    window_bounds=[[*window_bound]],
                    resolution=500,
                    padding=padding,
                ),
                data_prep.selective_tile(
                    filepath="netcdf:misc/antarctic_ice_vel_phase_map_v01.nc:VY",
                    window_bounds=[[*window_bound]],
                    resolution=500,
                    padding=padding,
                ),
            ],
            axis=1,
        )
        if not use_whole_rema:
            W1_tile = data_prep.selective_tile(
                filepath="misc/REMA_100m_dem_filled.tif",
                window_bounds=[[*window_bound]],
                padding=padding,
            )
        elif use_whole_rema:
            print("Getting: misc/REMA_100m_dem_filled.tif ...", end="")
            with xr.open_rasterio("misc/REMA_100m_dem_filled.tif") as ds:
                W1_tile = np.expand_dims(a=ds.values, axis=0)
                # special zero padding for REMA, 10pixels on top, bottom, left and right
                W1_tile = np.pad(
                    array=W1_tile,
                    pad_width=[(0, 0), (0, 0), (10, 10), (10, 10)],
                    mode="constant",
                )
            print("Done!")

    return X_tile, W1_tile, W2_tile, W3_tile


# %%
X_tile, W1_tile, W2_tile, W3_tile = get_deepbedmap_model_inputs(
    window_bound=window_bound
)
print(X_tile.shape, W1_tile.shape, W2_tile.shape, W3_tile.shape)

# Build quilt package for datasets covering our test region
reupload = False
if reupload == True:
    quilt.build(package="weiji14/deepbedmap/model/test/W1_tile", path=W1_tile)
    quilt.build(package="weiji14/deepbedmap/model/test/W2_tile", path=W2_tile)
    quilt.build(package="weiji14/deepbedmap/model/test/W3_tile", path=W3_tile)
    quilt.build(package="weiji14/deepbedmap/model/test/X_tile", path=X_tile)
    quilt.push(package="weiji14/deepbedmap/model/test", is_public=True)


# %%
def plot_3d_view(
    img: np.ndarray,
    ax: matplotlib.axes._subplots.Axes,
    elev: int = 60,
    azim: int = 330,
    z_minmax: tuple = None,
    title: str = None,
    zlabel: str = None,
):
    """
    Creates a 3D perspective view plot of an elevation surface using matplotlib 3D.
    The elevation (elev) and azimuth (azim) angle will need to be set accordingly,
    here it is looking from the SouthWest (330deg) at an angle 60deg above the z-plane.

    There are also optional parameters to set an elevation (z) range using the z_minmax
    parameter, just provide a tuple in the form of (zmin, zmax). The resulting 3D plot's
    colours will also be scaled to this range, or to the input array's z-range if this
    z_minmax setting is left blank. You can also provide a title and z-axis label.
    """
    # Get x, y, z data, assuming image in NCHW format
    image = img[0, :, :, :]
    xx, yy = np.mgrid[0 : image.shape[1], 0 : image.shape[2]]
    zz = image[0, :, :]

    # Scale the plot colours according to some elevation(z) range
    zmin, zmax = z_minmax if z_minmax is not None else (img.min(), img.max())
    cm_norm = matplotlib.cm.colors.Normalize(vmin=zmin, vmax=zmax)

    # Make the 3D surface plot
    ax.view_init(elev=elev, azim=azim)
    ax.plot_surface(xx, yy, zz, cmap="BrBG", norm=cm_norm)

    # Set title for plot and z-axis label
    ax.set_title(label=f"{title}\n", fontsize=22)
    ax.set_zlabel(zlabel=f"\n\n{zlabel}", fontsize=16)
    ax.set_zlim(bottom=zmin, top=zmax)

    return ax


# %% [markdown]
# ### Plot figures of neural network raster inputs in 2D and 3D

# %%
fig, axarr = plt.subplots(nrows=1, ncols=4, squeeze=False, figsize=(16, 12))
axarr[0, 0].imshow(X_tile[0, 0, :, :], cmap="BrBG")
axarr[0, 0].set_title("BEDMAP2\n(1000m resolution)")
axarr[0, 1].imshow(W1_tile[0, 0, :, :], cmap="BrBG")
axarr[0, 1].set_title("Reference Elevation Model of Antarctica\n(100m resolution)")
axarr[0, 2].imshow(np.linalg.norm(W2_tile, axis=(0, 1)), cmap="BrBG")
axarr[0, 2].set_title("MEaSUREs Ice Speed\n(450m resolution)")
axarr[0, 3].imshow(W3_tile[0, 0, :, :], cmap="BrBG")
axarr[0, 3].set_title("Antarctic Snow Accumulation\n(1000m resolution)")
plt.show()

# %%
fig = plt.figure(figsize=plt.figaspect(1 / 4) * 2.5)
axarr = [fig.add_subplot(1, 4, i + 1, projection="3d") for i in range(4)]

plot_3d_view(
    img=X_tile,
    ax=axarr[0],
    title="BEDMAP2\n(1000m resolution)",
    zlabel="Elevation (metres)",
)
plot_3d_view(
    img=W1_tile,
    ax=axarr[1],
    title="Reference Elevation Model of Antarctica\n(100m resolution)",
    zlabel="Elevation (metres)",
)
plot_3d_view(
    img=np.expand_dims(np.linalg.norm(W2_tile, axis=1), axis=0),
    ax=axarr[2],
    title="MEaSUREs Surface Ice Speed\n(450m resolution)",
    zlabel="Surface Ice Speed (metres/year)",
)
plot_3d_view(
    img=W3_tile,
    ax=axarr[3],
    title="Antarctic Snow Accumulation\n(1000m resolution)",
    zlabel="Snow Accumulation (kg/m2/a)",
)

plt.tight_layout()
plt.show()

# %% [markdown]
# ## 1.3 Prepare other interpolated grids for comparison
#
# We'll also have two other grids (interpolated to spatial resolution of 250m) to compare with our DeepBedMap model's prediction.
# They are:
#
# - Bicubic interpolated BEDMAP2 (baseline)
# - Synthetic High Resolution Grid from [Graham et al. 2017](https://doi.org/10.5194/essd-9-267-2017)

# %%
cubicbedmap2 = skimage.transform.rescale(
    image=X_tile[0, 0, 1:-1, 1:-1].astype(np.int32),
    scale=4,  # 4x upscaling
    order=3,  # cubic interpolation
    mode="reflect",
    anti_aliasing=True,
    multichannel=False,
    preserve_range=True,
)
cubicbedmap2 = np.expand_dims(np.expand_dims(cubicbedmap2, axis=0), axis=0)
print(cubicbedmap2.shape)

# %%
S_tile = data_prep.selective_tile(
    filepath="model/hres.tif", window_bounds=[[*window_bound]], interpolate=False
)
print(S_tile.shape)
synthetic250 = skimage.transform.rescale(
    image=S_tile[0, 0, :, :].astype(np.int32),
    scale=1 / 2.5,  # 2.5 downscaling
    order=1,  # billinear interpolation
    mode="reflect",
    anti_aliasing=True,
    multichannel=False,
    preserve_range=True,
)
synthetic250 = np.expand_dims(np.expand_dims(synthetic250, axis=0), axis=0)
print(synthetic250.shape)


# %% [markdown]
# # 2. Predict Bed Elevation

# %% [markdown]
# ## 2.1 Load trained generator neural network
#
# Fully convolutional networks rock!!
# Since we have a fully convolutional model architecture,
# we can use the same trained weights on different sized inputs/outputs!
# That way we can predict directly on an arbitrarily sized window.

# %%
def load_trained_model(
    experiment_key: str = "055b697548e048b78202cfebb78d6d8c",  # or simply use "latest"
    model_weights_path: str = "model/weights/srgan_generator_model_weights.npz",
):
    """
    Returns a trained Generator DeepBedMap neural network model.

    The model's weights and hyperparameters settings are retrieved from
    https://comet.ml/weiji14/deepbedmap using an `experiment_key` setting
    which can be set to 'latest' or some 32-character alphanumeric string.
    """
    srgan_train = _load_ipynb_modules("srgan_train.ipynb")

    # Download either 'latest' model weights from Comet.ML or one using experiment_key
    # Will also get the hyperparameters "num_residual_blocks" and "residual_scaling"
    num_residual_blocks, residual_scaling = _download_model_weights_from_comet(
        experiment_key=experiment_key, download_path=model_weights_path
    )

    # Architect the model with appropriate "num_residual_blocks" and "residual_scaling"
    model = srgan_train.GeneratorModel(
        num_residual_blocks=num_residual_blocks, residual_scaling=residual_scaling
    )

    # Load trained neural network weights into model
    chainer.serializers.load_npz(file=model_weights_path, obj=model)

    return model


# %%
model = load_trained_model()

# %% [markdown]
# ## 2.2 Make prediction on area of interest

# %%
Y_hat = model.forward(x=X_tile, w1=W1_tile, w2=W2_tile, w3=W3_tile).array

# %% [markdown]
# ### Plot DeepBedMap prediction alongside other interpolated grids and groundtruth in 2D and 3D

# %%
fig, axarr = plt.subplots(nrows=1, ncols=4, squeeze=False, figsize=(22, 12))
axarr[0, 0].imshow(X_tile[0, 0, :, :], cmap="BrBG")
axarr[0, 0].set_title("BEDMAP2")
axarr[0, 1].imshow(Y_hat[0, 0, :, :], cmap="BrBG")
axarr[0, 1].set_title("Super Resolution Generative Adversarial Network prediction")
axarr[0, 2].imshow(S_tile[0, 0, :, :], cmap="BrBG")
axarr[0, 2].set_title("Synthetic High Resolution Grid")
groundtruth.plot(ax=axarr[0, 3], cmap="BrBG")
axarr[0, 3].set_title("Groundtruth grids")
plt.show()

# %%
fig = plt.figure(figsize=plt.figaspect(12 / 9) * 4.5)  # (height/width)*scaling
axarr = [fig.add_subplot(2, 2, i + 1, projection="3d") for i in range(4)]

plot_3d_view(
    img=Y_hat,  # DeepBedMap
    ax=axarr[0],
    z_minmax=(Y_hat.min(), Y_hat.max()),
    title="DeepBedMap (ours)\n250m resolution",
    zlabel="Elevation (metres)",
)
plot_3d_view(
    img=X_tile,  # BEDMAP2
    ax=axarr[1],
    z_minmax=(Y_hat.min(), Y_hat.max()),
    title="BEDMAP2\n1000m resolution",
    zlabel="Elevation (metres)",
)
plot_3d_view(
    img=Y_hat - cubicbedmap2,  # DeepBedMap - BEDMAP2
    ax=axarr[2],
    z_minmax=(-125, 125),
    title="DeepBedMap minus\nBEDMAP2 (cubic interpolated)",
    zlabel="Difference (metres)",
)
plot_3d_view(
    img=S_tile,  # Synthetic High Resolution product
    ax=axarr[3],
    z_minmax=(Y_hat.min(), Y_hat.max()),
    title="Synthetic HRES\n100m resolution",
    zlabel="Elevation (metres)",
)

plt.subplots_adjust(wspace=0.0001, hspace=0.0001, left=0.0, right=0.9, top=1.2)
# plt.savefig(fname="esrgan_prediction.pdf", format="pdf", bbox_inches="tight")
plt.show()

# %% [markdown]
# ## 2.3 Save interpolated grids to GeoTIFF and NetCDF format
#
# 250 spatial resolution grids of:
# - DeepBedMap'3' (ours)
# - Bicubic interpolated BEDMAP2 (originally 1000m)
# - Bilinear interpolated Synthetic High Res (originally 100m)

# %%
# Save BEDMAP3 to GeoTiff and NetCDF format
deepbedmap3_grid = data_prep.save_array_to_grid(
    outfilepath="model/deepbedmap3",
    window_bound=window_bound,
    array=Y_hat[0, :, :, :],
    save_netcdf=True,
)
deepbedmap3_grid = xr.DataArray(
    data=np.flipud(cupy.asnumpy(Y_hat[0, 0, :, :])),
    dims=["y", "x"],
    coords={"y": deepbedmap3_grid.y, "x": deepbedmap3_grid.x},  # for multiple grids
    # coords={"y": groundtruth.y, "x": groundtruth.x},  # for single grid
)

# %%
# Save Bicubic Resampled BEDMAP2 to GeoTiff and NetCDF format
_ = data_prep.save_array_to_grid(
    outfilepath="model/cubicbedmap",
    window_bound=window_bound,
    array=cubicbedmap2[0, :, :, :],
    save_netcdf=True,
)
# Save Billinear Resampled Synthetic High Resolution grid to GeoTiff and NetCDF format
_ = data_prep.save_array_to_grid(
    outfilepath="model/synthetichr",
    window_bound=window_bound,
    array=synthetic250[0, :, :, :],
    save_netcdf=True,
)

# %% [markdown]
# # 3. Elevation 'error' analysis

# %% [markdown]
# Here we compare the elevation error (or difference) between our grid and many many points!
# We use [PyGMT](https://github.com/GenericMappingTools/pygmt)'s [grdtrack](https://gmt.soest.hawaii.edu/doc/latest/grdtrack) to sample the grid along the survey track points.
#
# The survey tracks are basically geographic points (x, y) with an elevation (z)
# that were collected from an airplane or ground vehicle crossing Antarctica.
# The four grids we sample from all have a spatial resolution of 250m and they are:
#
# - Groundtruth grid (interpolated from our groundtruth points using [surface](https://gmt.soest.hawaii.edu/doc/latest/surface.html))
# - DeepBedMap3 grid (predicted from our [Super Resolution Generative Adversarial Network model](/srgan_train.ipynb))
# - CubicBedMap grid (interpolated from BEDMAP2 using a [bicubic spline algorithm](http://scikit-image.org/docs/dev/api/skimage.transform.html#skimage.transform.rescale))
# - Synthetic High Res grid (created by [Graham et al. 2017](https://doi.org/10.5194/essd-9-267-2017))
#
# References:
#
# - Wessel, P., Smith, W. H. F., Scharroo, R., Luis, J., & Wobbe, F. (2013). Generic Mapping Tools: Improved Version Released. Eos, Transactions American Geophysical Union, 94(45), 409–410. https://doi.org/10.1002/2013EO450001
# - Wessel, P. (2010). Tools for analyzing intersecting tracks: The x2sys package. Computers & Geosciences, 36(3), 348–354. https://doi.org/10.1016/j.cageo.2009.05.009

# %%
tracks = [data_prep.ascii_to_xyz(pipeline_file=f"{pf}.json") for pf in test_filepaths]
points: pd.DataFrame = pd.concat(objs=tracks)  # concatenate all tracks into one table

# %%
df_groundtruth = gmt.grdtrack(
    points=points, grid=groundtruth, newcolname="z_interpolated"
)
# df_deepbedmap3 = gmt.grdtrack(
#     points=points, grid=deepbedmap3_grid, newcolname="z_interpolated"
# )
df_deepbedmap3 = gmt.grdtrack(
    points=points, grid="model/deepbedmap3.nc", newcolname="z_interpolated"
)
df_cubicbedmap = gmt.grdtrack(
    points=points, grid="model/cubicbedmap.nc", newcolname="z_interpolated"
)
df_synthetichr = gmt.grdtrack(
    points=points, grid="model/synthetichr.nc", newcolname="z_interpolated"
)

# %% [markdown]
# ### Get table statistics

# %%
df_groundtruth["error"] = df_groundtruth.z_interpolated - df_groundtruth.z
df_groundtruth.describe()

# %%
df_deepbedmap3["error"] = df_deepbedmap3.z_interpolated - df_deepbedmap3.z
df_deepbedmap3.describe()

# %%
df_cubicbedmap["error"] = df_cubicbedmap.z_interpolated - df_cubicbedmap.z
df_cubicbedmap.describe()

# %%
df_synthetichr["error"] = df_synthetichr.z_interpolated - df_synthetichr.z
df_synthetichr.describe()

# %% [markdown]
# ### Plot elevation error histogram

# %%
# https://medium.com/usf-msds/choosing-the-right-metric-for-machine-learning-models-part-1-a99d7d7414e4
rmse_groundtruth = (df_groundtruth.error ** 2).mean() ** 0.5
rmse_deepbedmap3 = (df_deepbedmap3.error ** 2).mean() ** 0.5
rmse_cubicbedmap = (df_cubicbedmap.error ** 2).mean() ** 0.5
rmse_synthetichr = (df_synthetichr.error ** 2).mean() ** 0.5

bins = 50

fig, ax = plt.subplots(figsize=(16, 9))
# ax.set_yscale(value="symlog")
ax.set_xlim(left=-150, right=100)
df_groundtruth.hist(
    column="error",
    bins=25,
    ax=ax,
    histtype="step",
    label=f"Groundtruth RMSE: {rmse_groundtruth:.2f}",
)
df_deepbedmap3.hist(
    column="error",
    bins=25,
    ax=ax,
    histtype="step",
    label=f"DeepBedMap RMSE: {rmse_deepbedmap3:.2f}",
)
df_cubicbedmap.hist(
    column="error",
    bins=25,
    ax=ax,
    histtype="step",
    label=f"CubicBedMap RMSE: {rmse_cubicbedmap:.2f}",
)
"""
df_synthetichr.hist(
    column="error",
    bins=bins,
    ax=ax,
    histtype="step",
    label=f"SyntheticHR RMSE: {rmse_synthetichr:.2f}",
)
"""

ax.set_title("Elevation error between interpolated bed and groundtruth", fontsize=32)
ax.set_xlabel("Error in metres", fontsize=32)
ax.set_ylabel("Number of data points", fontsize=32)
ax.legend(loc="upper left", fontsize=32)
plt.tick_params(axis="both", labelsize=20)
plt.axvline(x=0)

# plt.savefig(fname="elevation_error_histogram.pdf", format="pdf", bbox_inches="tight")
plt.show()

# %%
print(f"Groundtruth RMSE: {rmse_groundtruth}")
print(f"DeepBedMap3 RMSE: {rmse_deepbedmap3}")
print(f"SyntheticHR RMSE: {rmse_synthetichr}")
print(f"CubicBedMap RMSE: {rmse_cubicbedmap}")
print(f"Difference      : {rmse_deepbedmap3 - rmse_cubicbedmap}")

# %% [markdown]
# # 4. Antarctic-wide **DeepBedMap**
#
# Using our neural network to predict the bed elevation of the whole Antarctic continent!
# A previous version (April 2019) presented at EGU2019 can be found in this [issue](https://github.com/weiji14/deepbedmap/issues/133) with reproducible code in this [pull request](https://github.com/weiji14/deepbedmap/pull/136).

# %%
# Bounding Box region in EPSG:3031 covering Antarctica
window_bound_big = rasterio.coords.BoundingBox(
    left=-2_700_000.0, bottom=-2_200_000.0, right=2_800_000.0, top=2_300_000.0
)
print(window_bound_big)

# %%
try:
    X_tile = np.load(file="X_tile_big.npy")
    W1_tile = np.load(file="W1_tile_big.npy")
    W2_tile = np.load(file="W2_tile_big.npy")
    W3_tile = np.load(file="W3_tile_big.npy")
except FileNotFoundError:
    X_tile, W1_tile, W2_tile, W3_tile = get_deepbedmap_model_inputs(
        window_bound=window_bound_big, use_whole_rema=True
    )
    np.save(file="X_tile_big.npy", arr=X_tile)
    np.save(file="W1_tile_big.npy", arr=W1_tile)
    np.save(file="W2_tile_big.npy", arr=W2_tile)
    np.save(file="W3_tile_big.npy", arr=W3_tile)

# %%
# Oh we will definitely need a GPU for this
os.environ["CUDA_VISIBLE_DEVICES"] = "1"
model.to_gpu()

# %%
# clip Ice Surface Elevation, Velocity and Accummulation values to above 0.0
W1_tile = np.clip(a=W1_tile, a_min=0.0, a_max=None)
W2_tile = np.clip(a=W2_tile, a_min=0.0, a_max=None)
W3_tile = np.clip(a=W3_tile, a_min=0.0, a_max=None)

# %%
print(X_tile.shape, W1_tile.shape, W2_tile.shape, W3_tile.shape)


# %% [markdown]
# ## 4.1 The whole of Antarctica tiler and predictor!!
#
# Antarctica won't fit into our 16GB of GPU memory, so we have to:
#
# 1. Cut a 250kmx250km tile and load the data within this one small tile into GPU memory
# 2. Use our GPU-enabled model to make a prediction for this tile area
# 3. Repeat (1) and (2) for every tile we have covering Antarctica

# %%
@dataclasses.dataclass(frozen=True)
class Shape:
    y: int  # size in y-direction
    x: int  # size in x-direction


# %%
# The whole of Antarctica tile and predictor
with chainer.using_config(name="cudnn_deterministic", value=True):
    # Size are in kilometres, same as BEDMAP2's 1km resolution
    final_shape = Shape(y=18000, x=22000)  # 4x that of BEDMAP2
    ary_shape = Shape(y=1000, x=1000)  # 1000pixels * 250m = 250km
    stride = Shape(y=1000, x=1000)  # cut a tile every 1000 metres
    xtrapad = Shape(y=18, x=18)  # extra padding at borders (which will be clipped off)

    Y_hat = np.full(
        shape=(1, final_shape.y, final_shape.x), fill_value=np.nan, dtype=np.float32
    )

    steps = []
    for y_step in range(0, final_shape.y, stride.y):
        for x_step in range(0, final_shape.x, stride.x):
            steps.append(Shape(y=y_step, x=x_step))

    for step in steps:
        # plus `xtrapad.y` pixels and 1 pixel on bottom and top
        y0 = max(0, (step.y // 4) - xtrapad.y - 1)
        y1 = min(final_shape.y // 4, ((step.y + ary_shape.y) // 4) + xtrapad.y + 1)
        # plus `xtrapad.x` pixels and 1 pixel on left and right
        x0 = max(0, (step.x // 4) - xtrapad.x - 1)
        x1 = min(final_shape.x // 4, ((step.x + ary_shape.x) // 4) + xtrapad.x + 1)
        # print(str(x0).zfill(4), str(x1).zfill(4), str(y0).zfill(4), str(y1).zfill(4))

        # Hardcoded crops of BEDMAP2 (X), REMA (W1), MEaSUREs (W2), Accumulation (W3)
        X_tile_crop = model.xp.asarray(a=X_tile[:, :, y0:y1, x0:x1], dtype="float32")
        W1_tile_crop = model.xp.asarray(
            a=W1_tile[:, :, y0 * 10 : y1 * 10, x0 * 10 : x1 * 10], dtype="float32"
        )
        W2_tile_crop = model.xp.asarray(
            a=W2_tile[:, :, y0 * 2 : y1 * 2, x0 * 2 : x1 * 2], dtype="float32"
        )
        W3_tile_crop = model.xp.asarray(a=W3_tile[:, :, y0:y1, x0:x1], dtype="float32")

        # DeepBedMap terrain inference
        with chainer.using_config(name="enable_backprop", value=False):
            Y_pred = model.forward(
                x=X_tile_crop, w1=W1_tile_crop, w2=W2_tile_crop, w3=W3_tile_crop
            )

        try:
            y_slice = slice((y0 + xtrapad.y + 1) * 4, (y1 - xtrapad.y - 1) * 4)
            x_slice = slice((x0 + xtrapad.x + 1) * 4, (x1 - xtrapad.x - 1) * 4)
            Y_hat[:, y_slice, x_slice] = cupy.asnumpy(
                Y_pred.array[
                    0, :, xtrapad.y * 4 : -xtrapad.y * 4, xtrapad.x * 4 : -xtrapad.x * 4
                ]
            )
        except ValueError:
            raise
        finally:
            X_tile_crop = W1_tile_crop = W2_tile_crop = W3_tile_crop = None


# %% [markdown]
# ## 4.2 Save full map to file

# %%
# Save BEDMAP3 to GeoTiff and NetCDF format
# Using LZW compression and int16 instead of float32 to keep things smaller
_ = data_prep.save_array_to_grid(
    window_bound=window_bound_big,
    array=Y_hat.astype(dtype=np.int16),
    outfilepath="model/deepbedmap3_big_int16",
    dtype=np.int16,
    tiled=True,
    compression=rasterio.enums.Compression.lzw.value,  # Lempel-Ziv-Welch, lossless
)

# %% [markdown]
# ## 4.3 Show *the* DeepBedMap

# %%
# Adapted from https://docs.generic-mapping-tools.org/latest/gallery/ex42.html
fig = gmt.Figure()
#!gmt makecpt -Coleron -T-4500/4500 > deepbedmap.cpt
fig.grdimage(
    grid="model/deepbedmap3_big_int16.tif",
    region="-2800000/2800000/-2800000/2800000",  # make it square!
    projection="x1:60000000",
    frame="afg",  # add minor tick labels
    C="deepbedmap.cpt",
    Q=True,
)
fig.coast(
    # region="-180/180/-90/-60",
    projection="s0/-90/-71/1:60000000",
    # frame="afg",  # add minor tick labels
    area_thresh="+ai",  # crop to Antarctic 'i'ce shelf boundary
    water="27/40/91",
    V="q",
)
fig.savefig(fname="deepbedmap.png")
fig.show()
