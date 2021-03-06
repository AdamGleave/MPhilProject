/* $Id$ */
package uk.ac.cam.eng.ml.tcs27.compression;

/** An interface for functions from elements of type A to elements
  * of type B. */
public interface Function<A,B> {

  public B eval(A a);

}
