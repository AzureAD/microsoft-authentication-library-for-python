# Best Practices for Managing Dependency Versions

The best practices for managing dependency versions depend on
whether you are maintaining an application or a library.

## If you are maintaining an application

There are two suggestions that you shall follow.

1. Pinning dependencies in a production environment.

   This refers to explicitly declaring the exact version of your application's dependencies,
   including those of its dependencies (a.k.a. transitive dependencies).
   This ensures consistent and predictable deployments by preventing accidental upgrades that might introduce errors or regressions.

   To achieve this, the steps can be different, based on the tooling you choose.
   For example, if you are using the common `pip` tool,
   you shall specify the version like this `pip install PackageName==x.y.z`,
   where x.y.z is the latest stable version that you have tested with your app.

   Another approach is to start from a clean virtual environment,
   install all the direct dependencies using the method described above,
   and then run a `pip freeze > requirements.txt`.
   It will generate a requirements.txt file containing all your dependencies
   (including the transitive dependencies) pinned to their current versions.
   Now you commit that requirements.txt.
   From now on, you can consistently recreate your production environment by
   `pip install -r requirements.txt`.

   You may also use advanced dependency management tools to simplify the work flow.
   For example,
   [pip-tools](https://pypi.org/project/pip-tools).

2. Keep the dependencies updated.

   The purpose of pinning dependencies in the production is not about
   sticking with old versions forever.
   It is about allowing you to control when to update what.
   In the long run, you are still recommended to keep the dependencies updated because:

   * The digital landscape is rife with threats.
     Outdated dependencies are prime targets for malicious actors.
     By staying updated, you fortify your project against known vulnerabilities
     and significantly reduce the risk of exploitation.
   * Software is rarely perfect. New versions of dependencies often include bug fixes,
     improving the stability and performance of your application.
     Ignoring updates means missing out on these enhancements.

   You shall maintain a non-production environment,
   periodically upgrade its dependencies to latest versions, test it out.
   If the test passes, you can update the requirements.txt accordingly,
   and deploy it to your production environment.

   <!--
   The content above is equivalent to
   [section 2 of this document](https://www.geeksforgeeks.org/best-practices-for-managing-python-dependencies/#2-use-a-requirementstxt-file).
   -->

## If you are maintaining a library

If you are maintaining a library (meaning it will be used by its downstream applications),
you shall avoid pinning your dependencies.
Instead, you shall declare your dependencies by range, such as `msal>=1.33, <2.0`.
The lower bound is the smallest version that started to provide the API you are using,
the upper bound is the version that is expected to contain breaking changes.

