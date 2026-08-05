{
  pkgs ? import <nixpkgs> { },
}:
{
  package = pkgs.callPackage ./package.nix { };
}
